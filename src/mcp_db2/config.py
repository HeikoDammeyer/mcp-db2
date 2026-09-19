"""Configuration, read from environment / .env with the DB2_ prefix."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DB2_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- connection ---
    hostname: str
    port: int = 50000
    database: str
    uid: str
    pwd: SecretStr

    # TLS. If security is SSL, port must be the SSL port.
    security: str | None = None
    sslservercertificate: str | None = None

    # --- safety limits ---
    max_rows: int = Field(default=200, ge=1)
    max_rows_limit: int = Field(default=5000, ge=1)
    query_timeout: int = Field(default=30, ge=1, le=600)
    pool_size: int = Field(default=4, ge=1, le=32)
    max_cell_chars: int = Field(default=4000, ge=100)

    # --- schema visibility ---
    # NoDecode: these arrive as plain comma-separated strings, not JSON.
    schema_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    schema_denylist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SYS*", "NULLID", "SQLJ"]
    )

    # --- HTTP transport ---
    # Shared secret every HTTP request must present as `Authorization: Bearer <token>`.
    http_token: SecretStr | None = None
    # Bind address for --transport http. Localhost by default; the container sets 0.0.0.0.
    http_bind: str = "127.0.0.1"
    # Host header values accepted over HTTP, e.g. "db2mcp.intern:*". See require_http_token.
    http_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("schema_allowlist", "schema_denylist", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [part.strip().upper() for part in v.split(",") if part.strip()]
        return v

    @field_validator("http_allowed_hosts", mode="before")
    @classmethod
    def _split_csv_keep_case(cls, v: object) -> object:
        """Same splitting as _split_csv, but host names keep their case."""
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    def connection_string(self) -> str:
        """Build the CLI connection string. Never log the result — it contains the password."""
        parts = [
            f"DATABASE={self.database}",
            f"HOSTNAME={self.hostname}",
            f"PORT={self.port}",
            "PROTOCOL=TCPIP",
            f"UID={self.uid}",
            f"PWD={self.pwd.get_secret_value()}",
        ]
        if self.security:
            parts.append(f"SECURITY={self.security}")
        if self.sslservercertificate:
            parts.append(f"SSLServerCertificate={self.sslservercertificate}")
        return ";".join(parts) + ";"

    def describe_target(self) -> str:
        """A safe, credential-free description of what we connect to."""
        tls = " (TLS)" if self.security else ""
        return f"{self.uid}@{self.hostname}:{self.port}/{self.database}{tls}"

    def schema_visible(self, name: str) -> bool:
        """Whether a schema may be shown/queried, per allow- and denylist.

        Catalog values arrive space-padded, so the name is stripped before matching —
        otherwise 'NULLID  ' would slip past the pattern 'NULLID'.
        """
        upper = name.strip().upper()
        if self.schema_allowlist and not any(fnmatch(upper, p) for p in self.schema_allowlist):
            return False
        return not any(fnmatch(upper, p) for p in self.schema_denylist)

    def schema_sql_filter(self, column: str) -> tuple[str, list[str]]:
        """A SQL predicate restricting `column` to visible schemas, plus its parameters.

        Filtering in SQL rather than in Python matters for catalog-wide searches: a row cap
        applied before the filter would otherwise fill up with excluded schemas. RTRIM because
        catalog names arrive space-padded.
        """
        clauses: list[str] = []
        params: list[str] = []
        if self.schema_allowlist:
            ors = " OR ".join(f"RTRIM({column}) LIKE ?" for _ in self.schema_allowlist)
            clauses.append(f"({ors})")
            params.extend(_to_like(p) for p in self.schema_allowlist)
        for pattern in self.schema_denylist:
            clauses.append(f"RTRIM({column}) NOT LIKE ?")
            params.append(_to_like(pattern))
        return (" AND ".join(clauses) if clauses else "1 = 1"), params

    def clamp_rows(self, requested: int | None) -> int:
        return min(requested or self.max_rows, self.max_rows_limit)

    def require_http_token(self) -> str:
        """The bearer token for the HTTP transport, or a hard failure.

        Over stdio the client is whoever launched the process; over HTTP it is anyone who can
        reach the port, so a server without a token must not start at all.
        """
        token = self.http_token.get_secret_value().strip() if self.http_token else ""
        if not token:
            raise ValueError(
                "DB2_HTTP_TOKEN is required for --transport http. "
                "Generate one with: openssl rand -hex 32"
            )
        if not token.isascii():
            # secrets.compare_digest rejects non-ASCII str, which would turn every request
            # into a 500 instead of a clean 401.
            raise ValueError("DB2_HTTP_TOKEN must contain only ASCII characters.")
        return token


def _to_like(pattern: str) -> str:
    """fnmatch pattern to SQL LIKE. `%` and `_` in the pattern keep their LIKE meaning."""
    return pattern.replace("*", "%").replace("?", "_")
