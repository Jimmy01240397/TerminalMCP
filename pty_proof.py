"""Proof that the spawned process really sees a TTY (the whole point of PTY mode).

Compares isatty(stdout) under terminal-mcp vs. a vanilla subprocess.
"""
from __future__ import annotations

import asyncio
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
    token = secrets.token_hex(8)
    url = f"http://127.0.0.1:{port}/mcp/"

    env = dict(os.environ)
    env["TERMINAL_MCP_HOST"] = "127.0.0.1"
    env["TERMINAL_MCP_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "terminal_mcp"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
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

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()

                # Through PTY: isatty(stdout) should be True.
                r1 = await s.call_tool(
                    "run",
                    {"cmd": "python3 -c 'import sys; print(\"isatty=\" + str(sys.stdout.isatty()))'"},
                )
                sid = r1.structuredContent["session_id"]
                await asyncio.sleep(0.3)
                r2 = await s.call_tool("output", {"session_id": sid})
                pty_out = r2.structuredContent["content"]

                # Also a gdb-style flush test: print without newline must arrive promptly
                # because the PTY puts stdout in line/none-buffered mode for the child.
                r3 = await s.call_tool(
                    "run",
                    {
                        "cmd": "python3 -u -c 'import sys, time; "
                        "sys.stdout.write(\"prompt> \"); sys.stdout.flush(); "
                        "time.sleep(0.05); sys.stdout.write(\"done\\n\")'"
                    },
                )
                sid3 = r3.structuredContent["session_id"]
                await asyncio.sleep(0.4)
                r4 = await s.call_tool("output", {"session_id": sid3})
                flush_out = r4.structuredContent["content"]

        # Compare to vanilla subprocess (no PTY).
        plain = subprocess.run(
            ["python3", "-c", "import sys; print('isatty=' + str(sys.stdout.isatty()))"],
            capture_output=True,
            text=True,
        ).stdout.strip()

        print(f"Through PTY MCP : {pty_out.strip()!r}")
        print(f"Vanilla pipe    : {plain!r}")
        print(f"Unbuffered prompt arrives intact: {flush_out!r}")

        ok = "isatty=True" in pty_out and "isatty=False" in plain and "prompt>" in flush_out and "done" in flush_out
        print("\nPTY proof:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
