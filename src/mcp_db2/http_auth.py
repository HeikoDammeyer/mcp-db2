"""Bearer-token gate for the HTTP transport.

Plain ASGI rather than Starlette middleware, because it has to sit *outside* the app the SDK
builds: `@mcp.custom_route` handlers bypass the SDK's own auth, and the health check is the
one endpoint that should stay reachable without a token.
"""

from __future__ import annotations

import logging
from secrets import compare_digest

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

#: Paths served without a token. Keep this to endpoints that reveal nothing.
DEFAULT_EXEMPT = frozenset({"/healthz"})


class BearerGate:
    """Rejects any HTTP request that does not carry the configured bearer token."""

    def __init__(
        self,
        app: ASGIApp,
        token: str,
        exempt: frozenset[str] = DEFAULT_EXEMPT,
    ) -> None:
        if not token.isascii():
            raise ValueError("Bearer token must be ASCII.")
        self.app = app
        self.exempt = exempt
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt:
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization", "")
        # Compare the whole header so the runtime does not depend on how much of the token
        # matched. A non-ASCII header would make compare_digest raise, so it is rejected first.
        if not header.isascii() or not compare_digest(header, self._expected):
            log.warning("Rejected unauthorized %s %s", scope.get("method"), scope.get("path"))
            response = PlainTextResponse(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
