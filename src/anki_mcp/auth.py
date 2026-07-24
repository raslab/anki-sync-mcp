from __future__ import annotations

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    """Authenticate every request under the configured MCP endpoint."""

    def __init__(self, app: ASGIApp, token: str, mcp_path: str = "/mcp") -> None:
        self.app = app
        self._expected = token.encode("utf-8")
        self._path = mcp_path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == self._path or path.startswith(self._path + "/")):
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"")
            scheme, separator, supplied = authorization.partition(b" ")
            has_bearer_scheme = separator == b" " and scheme.lower() == b"bearer"
            # Always execute compare_digest, including malformed/missing credentials.
            valid = has_bearer_scheme and hmac.compare_digest(supplied, self._expected)
            if not valid:
                response = JSONResponse(
                    {"error": "authentication_failed"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
