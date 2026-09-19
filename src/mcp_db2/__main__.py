"""Entry point: `mcp-db2 [--transport stdio|http]`."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Settings
from .server import mcp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-db2", description="MCP server for Db2 LUW (read-only)"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio for a locally launched server (default), http for streamable HTTP.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP bind address. Defaults to DB2_HTTP_BIND (127.0.0.1).",
    )
    parser.add_argument("--port", type=int, default=3001, help="HTTP port.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    # stdout is the wire on stdio, so all logging goes to stderr.
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.transport == "stdio":
        mcp.run()
    else:
        _run_http(args.host, args.port)


def _run_http(host: str | None, port: int) -> None:
    """Serve streamable HTTP behind the bearer gate.

    Not `mcp.run(transport="streamable-http")`: that gives no way to wrap the app, and the
    token check has to sit outside everything the SDK mounts.
    """
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from .http_auth import BearerGate

    try:
        settings = Settings()  # type: ignore[call-arg]  # values come from env/.env
        token = settings.require_http_token()
    except ValueError as exc:
        print(f"Cannot start the HTTP transport: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    bind = host or settings.http_bind

    # The SDK turns DNS-rebinding protection on by itself only for a localhost bind, so on
    # 0.0.0.0 it must be configured here or it is simply off.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.http_allowed_hosts),
        allowed_hosts=settings.http_allowed_hosts,
        allowed_origins=[],
    )
    if bind not in ("127.0.0.1", "localhost", "::1") and not settings.http_allowed_hosts:
        logging.getLogger(__name__).warning(
            "Binding %s without DB2_HTTP_ALLOWED_HOSTS: Host header validation is disabled.",
            bind,
        )

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        host=bind,
        transport_security=security,
    )
    # log_config=None: keep the stderr logging configured above.
    uvicorn.run(BearerGate(app, token), host=bind, port=port, log_config=None)


if __name__ == "__main__":
    main()
