from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# Integration tests are gated on DB2_* being present in the environment. Load .env here so
# `uv run pytest -m integration` behaves like the server itself, which reads the same file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
