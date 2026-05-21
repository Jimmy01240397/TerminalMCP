---
name: terminal-mcp
description: How to drive the terminal-mcp PTY tools (run / input / output / output_history) when solving CTF challenges, debugging with gdb / pdb / lldb, running pwntools exploits, or interacting with any program that needs a real tty for output flushing. Use whenever the tools `run`, `input`, `output`, `output_history` are available and the task involves an interactive or buffered subprocess.
---

# Driving terminal-mcp

terminal-mcp gives you a real PTY for each subprocess, so `isatty(stdout)` is true inside the child and programs like gdb, REPLs, and pwntools-style targets flush their prompts immediately. The tradeoff vs the plain `Bash` tool: every call is async, you manage the lifecycle yourself, and output comes back with raw tty bytes (`\r\n`, ANSI escapes, echo).

## When to use this — and when NOT to

**Reach for `run` / `input` / `output` when:**

- The program is **interactive** — gdb / pdb / lldb, a REPL (`python3 -i`, `sqlite3`, `irb`), an exploit binary that reads from stdin between writes.
- The program **buffers stdout when not on a tty** — common with C programs using `printf` without `fflush`, gdb when stdout is a pipe, `nc` in some configs. Symptom: `Bash` returns no output, or output only appears at exit.
- You need to **stay attached** across many turns — set a breakpoint, run, inspect, step, inspect again. Each step is its own LLM turn.
- You want to **run several processes concurrently** — e.g. a debug session in one PTY and the target in another, talking to each other.

**Use `Bash` instead when:**

- The command **runs to completion and exits**, and you just want stdout (`ls`, `grep`, `make`, `pytest -x`, one-shot scripts). One Bash call beats four MCP calls every time.
- You just need file I/O, package installs, git, or other batch operations.

If you start with Bash and discover the program is waiting on stdin or buffering output, switch to `run`.

## Mental model

A session is an async PTY-backed child:

```
run(cmd) ─► session_id (returns immediately; cmd is running)
            │
            ├── input(session_id, data)        write bytes to its stdin
            ├── output(session_id)             drain everything written since last output()
            ├── output_history(sid, off, len)  random-access read of the full history
            └── close(session_id)              kill + forget
```

`run` is fire-and-forget. The shell is `/bin/sh -c <cmd>`. The session keeps existing after the process exits — `output` still works to drain leftover bytes, and `is_alive` / `exit_code` in the response tell you what happened. `close` only matters when you want to actively kill or free a session.

`output` is **destructive read**: it returns everything emitted since the last `output` call and clears the pending buffer. Call it once per "checkpoint." If you want to re-read, use `output_history` — it's a per-session ring buffer with absolute byte offsets (anchored to the lifetime of the stream, not the buffer's current contents). When the buffer overflows, `total_length` keeps growing but `buffer_start` advances past the dropped bytes.

## The standard loop

After you `input` something, the child needs a moment to react. Don't call `output` immediately and assume nothing arrived — give it ~200–500 ms (poll a couple of times if you need to be sure):

```
input(sid, "break main\n")
# brief pause — for slow targets, call output() 2–3 times spaced apart
out = output(sid)
# scan out["content"] for the expected prompt / pattern before sending the next input
```

For programs that print a recognisable prompt (`(gdb) `, `>>> `, `$ `), the right wait condition is "the latest output ends with the prompt." If you don't see the prompt yet, call `output` again. The PTY does flush — if a prompt doesn't appear, the program is genuinely still busy.

## Patterns

### gdb session

```
sid = run("gdb -q ./challenge")
# drain the startup banner
output(sid)                       # expect "(gdb) "
input(sid, "break *main+0x42\n")
output(sid)                       # confirms breakpoint
input(sid, "run\n")
output(sid)                       # "Breakpoint 1, ..." once it hits
input(sid, "info reg rax\n")
output(sid)                       # register value
# keep going across turns — the session persists
```

Keep the gdb session alive across the whole investigation. Don't `run("gdb ...")` again every turn — you'd start a new process and lose your breakpoints.

### pwntools-style local exploit

If the exploit fits a single Python script, do the easy thing: write the script and `run("python3 solve.py")`, then read output. Use terminal-mcp only when you want to **iterate live** — send a stage, observe, send next stage.

```
sid = run("./pwn-challenge")
output(sid)                       # menu
input(sid, "1\n")                 # pick option
output(sid)                       # prompt for payload
import base64
payload = b"A" * 40 + p64(0xdeadbeef)
input(sid, data=base64.b64encode(payload).decode(), binary=True)
output(sid, binary=True)          # raw bytes back — may include the leak
```

For binary I/O (raw payloads, leaks containing non-UTF-8 bytes), pass `binary=true` to both `input` and `output`. The content field becomes base64.

### Remote service over `nc`

```
sid = run("nc ctf.host 1337")
output(sid)                       # banner / menu
input(sid, "1\n")
output(sid)
# … or close and reconnect to reset state:
close(sid)
sid = run("nc ctf.host 1337")
```

### Long-running background job

Spawn it and check in periodically:

```
sid = run("./fuzzer --target ./bin --time 600")
# do other work in other sessions…
# later:
out = output(sid)                 # drain new findings since last check
# or get the whole picture:
hist = output_history(sid)        # total_length, buffer_start, content
```

For very chatty processes, the ring buffer will drop the oldest bytes — `output_history` still tells you `total_length` (how much was ever written) and `buffer_start` (offset of the oldest still-buffered byte). Use `offset` + `length` to page through what's still there.

### Two processes that talk to each other

Different sessions, same agent:

```
victim = run("./vulnerable-service --port 9999")
output(victim)                    # wait for "listening on 9999"
attacker = run("./exploit-client 127.0.0.1 9999")
output(attacker)
output(victim)                    # see what the service got
```

## Gotchas

- **`\r\n` line endings everywhere.** PTYs convert `\n` to `\r\n` on output. When pattern-matching, either strip `\r` or include it in the pattern.
- **Your input echoes.** A typical PTY echoes stdin to stdout. So `input(sid, "ls\n")` followed by `output(sid)` returns something like `ls\r\nfile1 file2\r\n$ `. The first line is your own echo. To suppress echo, send `stty -echo\n` after `run`.
- **`output` clears pending.** Calling it twice in a row gives empty content the second time. Use `output_history` if you want to re-read.
- **Sessions outlive their process.** After the child exits, `is_alive=false` and `exit_code` is set, but the session and its history are still there. Call `close` when you're really done — otherwise sessions and ring buffers accumulate.
- **`run` returns before the child has produced any output.** If you `run("echo hi")` and immediately `output`, you may get an empty string. Either call `output` 2–3 times with brief gaps, or check `is_alive` — once it's false you've definitely got everything.
- **Bearer-token isolation.** `list_sessions` only shows your own sessions — you can't see or steal another agent's. If you expect a session to be there and it isn't, you may be talking to the server with a different token than you think.
- **Window size matters for some tools.** Programs like `less`, `vim`, gdb's TUI render based on `$LINES` / `$COLUMNS`. The default is 24×80. If output gets paginated or wraps oddly, `resize(sid, rows, cols)` to something bigger (e.g. 50×200).
- **For control characters** (Ctrl-C, Ctrl-D, Ctrl-Z), send the raw byte: `input(sid, "\x03")` for Ctrl-C, `"\x04"` for EOF. You can also use `send_signal(sid, 2)` for SIGINT to the process group.

## Quick reference

| Tool | Purpose |
|---|---|
| `run(cmd, cwd?, env?, rows?, cols?)` | Spawn `/bin/sh -c <cmd>` in a fresh PTY. Returns `{session_id, pid}`. |
| `input(session_id, data, binary?)` | Write to stdin. `binary=true` → `data` is base64. |
| `output(session_id, binary?)` | Drain pending output. Returns `{content, bytes, is_alive, exit_code, exit_signal}`. |
| `output_history(session_id, offset?, length?, binary?)` | Random-access read. Returns `{content, content_offset, bytes, total_length, buffer_start}`. Negative `offset` = from end. `length` null / -1 = until end. |
| `list_sessions()` | Inventory your sessions. |
| `close(session_id, sig?)` | Send signal (default SIGKILL) and remove. |
| `send_signal(session_id, sig)` | Send a signal without removing. |
| `resize(session_id, rows, cols)` | Change PTY window size. |
