"""Bearer-token auth middleware. Sets a contextvar that tools read."""
from __future__ import annotations

from contextvars import ContextVar

current_token: ContextVar[str | None] = ContextVar("terminal_mcp_token", default=None)


class AuthMiddleware:
    """ASGI middleware that extracts ``Authorization: Bearer <token>``.

    If ``allowed_tokens`` is provided, only listed tokens pass; otherwise any
    non-empty token is accepted (the token itself isolates agents — different
    tokens get different session pools).
    """

    def __init__(self, app, allowed_tokens: set[str] | None = None) -> None:
        self.app = app
        self.allowed_tokens = allowed_tokens

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("latin-1")
        token: str | None = None
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()

        if not token or (
            self.allowed_tokens is not None and token not in self.allowed_tokens
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="terminal-mcp"'),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"missing or invalid bearer token"}',
                }
            )
            return

        current_token.set(token)
        await self.app(scope, receive, send)
