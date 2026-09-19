# linux/amd64 only: ibm_db ships manylinux wheels for x86_64 and i686, none for aarch64.
# On an Apple Silicon machine, build and run with --platform linux/amd64 (emulated).
FROM --platform=linux/amd64 python:3.11-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so a source change does not re-resolve the environment.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev


FROM --platform=linux/amd64 python:3.11-slim-bookworm

# The clidriver bundled in the ibm_db wheel links against libxml2 at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 mcp
WORKDIR /app

COPY --from=build --chown=mcp:mcp /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DB2_HTTP_BIND=0.0.0.0

# Fail the build rather than the first request if the driver cannot load here.
RUN python -c "import ibm_db"

USER mcp
EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3001/healthz || exit 1

ENTRYPOINT ["mcp-db2", "--transport", "http", "--port", "3001"]
