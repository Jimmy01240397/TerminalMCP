"""Bearer-token auth middleware. Sets a contextvar that tools read."""
from __future__ import annotations

from contextvars import ContextVar

# The sentinel agent id for requests that don't carry a bearer token. All
# such requests share one session pool — they're indistinguishable from the
# server's perspective.
ANONYMOUS_AGENT = "<anonymous>"

current_token: ContextVar[str | None] = ContextVar("terminal_mcp_token", default=None)


class AuthMiddleware:
    """ASGI middleware that extracts ``Authorization: Bearer <token>``.

    Two modes:

    - **No whitelist** (``allowed_tokens is None``, the default): every request
      is accepted. A non-empty bearer becomes the agent id (isolates its own
      session pool); an absent or empty bearer falls through to a shared
      :data:`ANONYMOUS_AGENT` pool. This lets clients that don't pre-flight
      with auth headers (e.g. some ``claude mcp add`` probes) still connect.
    - **Whitelist** (``allowed_tokens`` is a set): only listed tokens pass.
      Missing or unlisted bearer → 401.
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
        token = ""
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()

        if self.allowed_tokens is not None:
            # Whitelist mode: token must be present and listed.
            if not token or token not in self.allowed_tokens:
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

        current_token.set(token or ANONYMOUS_AGENT)
        await self.app(scope, receive, send)
