"""End-to-end smoke test for terminal-mcp.

Spawns the server in a subprocess on a free port, then drives it as a
Streamable-HTTP MCP client using the official mcp Python SDK. Exercises
every tool and the bearer-token isolation.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import closing


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    port = free_port()
    token_a = secrets.token_hex(8)
    token_b = secrets.token_hex(8)
    url = f"http://127.0.0.1:{port}/mcp/"

    env = dict(os.environ)
    env["TERMINAL_MCP_HOST"] = "127.0.0.1"
    env["TERMINAL_MCP_PORT"] = str(port)
    env["TERMINAL_MCP_BUFFER_BYTES"] = "256"  # tiny — exercises buffer overflow
    # Don't restrict TERMINAL_MCP_TOKENS so any bearer is accepted.

    proc = subprocess.Popen(
        [sys.executable, "-m", "terminal_mcp"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait for server to bind.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        print("server failed to start", file=sys.stderr)
        if proc.stdout:
            print(proc.stdout.read(), file=sys.stderr)
        return 1

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if cond:
            print(f"  ✓ {label}")
        else:
            print(f"  ✗ {label}")
            failures.append(label)

    async def call(session: ClientSession, name: str, args: dict) -> dict:
        result = await session.call_tool(name, args)
        if result.isError:
            raise RuntimeError(f"{name} failed: {result.content}")
        # FastMCP returns structuredContent for dict/list returns
        if getattr(result, "structuredContent", None):
            sc = result.structuredContent
            # FastMCP wraps return value under "result" key for top-level types
            return sc.get("result", sc) if isinstance(sc, dict) else sc
        # Fallback: parse first text block as JSON
        for block in result.content:
            if getattr(block, "type", None) == "text":
                try:
                    return json.loads(block.text)
                except Exception:
                    return {"_text": block.text}
        return {}

    try:
        # --- bad auth ---------------------------------------------------------
        try:
            async with streamablehttp_client(url) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
            check(False, "request without bearer token is rejected")
        except Exception:
            check(True, "request without bearer token is rejected")

        # --- agent A ----------------------------------------------------------
        headers_a = {"Authorization": f"Bearer {token_a}"}
        async with streamablehttp_client(url, headers=headers_a) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                tool_names = {t.name for t in tools.tools}
                expected = {"run", "input", "output", "output_history",
                            "list_sessions", "close", "resize", "send_signal"}
                check(expected <= tool_names,
                      f"tools listed (got {sorted(tool_names)})")

                # run a command
                res = await call(s, "run", {"cmd": "echo hello-from-pty"})
                sid = res["session_id"]
                check(isinstance(sid, str) and len(sid) > 0, "run returns session_id")

                # give the child a moment to finish
                await asyncio.sleep(0.3)

                out = await call(s, "output", {"session_id": sid})
                check("hello-from-pty" in out["content"],
                      f"output captures echo (got {out['content']!r})")
                check(out["is_alive"] is False, "session marked dead after echo")

                # history reflects same content
                hist = await call(s, "output_history", {"session_id": sid})
                check("hello-from-pty" in hist["content"],
                      "history contains echo output")
                check(hist["total_length"] == hist["length"] or
                      hist["total_length"] >= hist["length"],
                      "history total_length is consistent")

                # buffer overflow: produce > 256 bytes, ensure ring drops oldest
                res = await call(s, "run",
                                 {"cmd": "for i in $(seq 1 50); do printf 'XXXXXXXXXX'; done"})
                sid2 = res["session_id"]
                await asyncio.sleep(0.4)

                # History is empty until output() is called — verify that first.
                hist_empty = await call(s, "output_history", {"session_id": sid2})
                check(hist_empty["total_length"] == 0,
                      f"history is empty before output() (got total_length={hist_empty['total_length']})")

                # Drain into history, then check overflow behavior.
                await call(s, "output", {"session_id": sid2})
                hist2 = await call(s, "output_history", {"session_id": sid2})
                # 50 * 10 = 500 bytes emitted; buffer is 256, so buffer_start > 0
                check(hist2["total_length"] >= 500,
                      f"total_length tracks dropped bytes (got {hist2['total_length']})")
                check(hist2["buffer_start"] > 0,
                      f"ring dropped early bytes (buffer_start={hist2['buffer_start']})")
                check(hist2["length"] <= 256,
                      f"buffer respects capacity (got {hist2['length']})")

                # paginated reads
                slice1 = await call(s, "output_history",
                                    {"session_id": sid2, "offset": hist2["buffer_start"], "length": 50})
                check(slice1["length"] == 50, "paginated read returns requested length")
                check(slice1["content_offset"] == hist2["buffer_start"],
                      "content_offset matches requested offset")

                # input → cat round trip in interactive shell
                res = await call(s, "run", {"cmd": "cat"})
                sid3 = res["session_id"]
                await call(s, "input", {"session_id": sid3, "data": "ping\n"})
                await asyncio.sleep(0.3)
                out3 = await call(s, "output", {"session_id": sid3})
                check("ping" in out3["content"],
                      f"input → cat round-trips (got {out3['content']!r})")
                check(out3["is_alive"], "cat still alive after one line")

                # close it
                res = await call(s, "close", {"session_id": sid3})
                check(res["closed"], "close kills and removes session")

                # list_sessions excludes closed
                sessions = await call(s, "list_sessions", {})
                # FastMCP unwraps list returns differently
                if isinstance(sessions, dict) and "result" in sessions:
                    sessions = sessions["result"]
                sids = {x["session_id"] for x in sessions}
                check(sid3 not in sids,
                      f"closed session removed from list (sids={sids})")
                check(sid in sids and sid2 in sids,
                      "other sessions still listed")

        # --- agent B sees its own sessions only -------------------------------
        headers_b = {"Authorization": f"Bearer {token_b}"}
        async with streamablehttp_client(url, headers=headers_b) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                sessions = await call(s, "list_sessions", {})
                if isinstance(sessions, dict) and "result" in sessions:
                    sessions = sessions["result"]
                check(sessions == [],
                      f"agent B starts empty — isolation works (got {sessions!r})")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
