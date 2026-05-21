"""FastMCP server exposing PTY tools over Streamable HTTP."""
from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .agent import AgentRegistry
from .auth import AuthMiddleware, current_token

DEFAULT_BUFFER_BYTES = 1 * 1024 * 1024


def _decode_text_or_b64(data: str, binary: bool) -> bytes:
    if binary:
        return base64.b64decode(data)
    return data.encode("utf-8")


def _encode_bytes(data: bytes, binary: bool) -> str:
    if binary:
        return base64.b64encode(data).decode("ascii")
    return data.decode("utf-8", errors="replace")


def build_app():
    """Build the ASGI app: FastMCP streamable HTTP + bearer-token middleware."""
    buffer_bytes = int(
        os.environ.get("TERMINAL_MCP_BUFFER_BYTES", str(DEFAULT_BUFFER_BYTES))
    )
    allowed_env = os.environ.get("TERMINAL_MCP_TOKENS", "").strip()
    allowed_tokens: set[str] | None
    if allowed_env:
        allowed_tokens = {t.strip() for t in allowed_env.split(",") if t.strip()}
    else:
        allowed_tokens = None

    registry = AgentRegistry(buffer_bytes)
    mcp = FastMCP("terminal-mcp")

    def _agent():
        token = current_token.get()
        if not token:
            raise RuntimeError(
                "no bearer token in request context — auth middleware not applied?"
            )
        return registry.get_or_create(token)

    # ---- tools ----------------------------------------------------------------

    @mcp.tool(
        description=(
            "Spawn a shell command inside a fresh PTY. Returns a session_id "
            "you pass to the other tools. The command runs as `/bin/sh -c <cmd>`."
        )
    )
    async def run(
        cmd: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> dict[str, Any]:
        agent = _agent()
        sess = await asyncio.to_thread(agent.create, cmd, cwd, env, rows, cols)
        return {"session_id": sess.id, "pid": sess.pid}

    @mcp.tool(
        name="input",
        description=(
            "Write data to a session's stdin (master end of the PTY). "
            "Pass `binary=true` to send base64-encoded bytes — useful for "
            "raw exploits or control characters."
        ),
    )
    async def input_(session_id: str, data: str, binary: bool = False) -> dict[str, Any]:
        agent = _agent()
        sess = agent.get(session_id)
        raw = _decode_text_or_b64(data, binary)
        written = await asyncio.to_thread(sess.write, raw)
        return {"bytes_written": written}

    @mcp.tool(
        description=(
            "Drain everything currently buffered in the session's PTY and "
            "return it. The drained bytes are also appended to the session's "
            "history ring buffer — `output` is the only thing that promotes "
            "bytes into history. Call this periodically on long-running "
            "sessions, otherwise the child eventually blocks on its own stdout "
            "(PTY kernel buffer fills up — deliberate backpressure)."
        )
    )
    async def output(session_id: str, binary: bool = False) -> dict[str, Any]:
        agent = _agent()
        sess = agent.get(session_id)
        drained = await asyncio.to_thread(sess.drain_and_record)
        return {
            "content": _encode_bytes(drained, binary),
            "bytes": len(drained),
            "is_alive": sess.is_alive,
            "exit_code": sess.exit_code,
            "exit_signal": sess.exit_signal,
        }

    @mcp.tool(
        description=(
            "Read from the session's history ring buffer. History only contains "
            "bytes that have been drained by an `output` call — fresh PTY bytes "
            "that no `output` has touched yet are not visible here. Offsets are "
            "absolute byte offsets into the lifetime *output()-drained* stream. "
            "If `offset` precedes the buffer's oldest still-held byte, the "
            "returned content begins at the buffer's start — `content_offset` "
            "tells you where it actually begins. `length` < 0 or null means "
            "'until the end'. `total_length` is the total bytes ever promoted "
            "into history, including those that have rolled out of the buffer."
        )
    )
    async def output_history(
        session_id: str,
        offset: int = 0,
        length: int | None = None,
        binary: bool = False,
    ) -> dict[str, Any]:
        agent = _agent()
        sess = agent.get(session_id)
        content, content_offset, total, buf_start = await asyncio.to_thread(
            sess.read_history, offset, length
        )
        return {
            "content": _encode_bytes(content, binary),
            "content_offset": content_offset,
            "bytes": len(content),
            "total_length": total,
            "buffer_start": buf_start,
        }

    @mcp.tool(description="List your current sessions (scoped to your bearer token).")
    async def list_sessions() -> list[dict[str, Any]]:
        agent = _agent()

        def _snapshot() -> list[dict[str, Any]]:
            out = []
            for s in agent.list():
                s.refresh()  # update is_alive / exit_code without draining
                out.append(
                    {
                        "session_id": s.id,
                        "cmd": s.cmd,
                        "pid": s.pid,
                        "is_alive": s.is_alive,
                        "exit_code": s.exit_code,
                        "exit_signal": s.exit_signal,
                        "history_bytes": s.history.total_written,
                    }
                )
            return out

        return await asyncio.to_thread(_snapshot)

    @mcp.tool(
        description=(
            "Force-kill the session (SIGKILL), reap the child, drain any final "
            "bytes into history, and remove the session from your list. Use "
            "`send_signal` first if you want to send a catchable signal "
            "(SIGTERM / SIGINT) and let the process clean up."
        )
    )
    async def close(session_id: str) -> dict[str, Any]:
        agent = _agent()
        sess = agent.remove(session_id)
        if sess is None:
            return {"closed": False}
        await asyncio.to_thread(sess.close)
        return {"closed": True, "session_id": session_id}

    @mcp.tool(
        description=(
            "Resize the PTY window. Useful for programs that draw a UI based "
            "on terminal dimensions (gdb's TUI, less, vim, top)."
        )
    )
    async def resize(session_id: str, rows: int, cols: int) -> dict[str, Any]:
        agent = _agent()
        sess = agent.get(session_id)
        await asyncio.to_thread(sess.resize, rows, cols)
        return {"rows": rows, "cols": cols}

    @mcp.tool(
        description=(
            "Send a Unix signal to a session without removing it (use `close` "
            "to also drop it from the registry)."
        )
    )
    async def send_signal(session_id: str, sig: int) -> dict[str, Any]:
        agent = _agent()
        sess = agent.get(session_id)
        sess.signal(sig)
        return {"ok": True}

    app = mcp.streamable_http_app()
    return AuthMiddleware(app, allowed_tokens=allowed_tokens)


def main() -> None:
    import uvicorn

    host = os.environ.get("TERMINAL_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("TERMINAL_MCP_PORT", "8765"))
    app = build_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
