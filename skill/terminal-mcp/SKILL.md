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

A session is an async PTY-backed child. Output is **read on demand** — there's no background thread; bytes sit in the OS's PTY kernel buffer until `output` pulls them out.

```
child stdout ─► PTY kernel buffer (~8 KB OS-managed)
                       │
                       │  (output() drains it: non-blocking read until empty)
                       ▼
                 history ring buffer  (cap = TERMINAL_MCP_BUFFER_BYTES, default 1 MiB)
                       │
                       ├─► returned to caller       (= output() return value)
                       └─► random-access by offset  (= output_history())
```

`run` is fire-and-forget — it `pty.fork()`s, execs `/bin/sh -c <cmd>`, and returns the `session_id` immediately. The session keeps existing after the process exits — `output` still drains any final bytes, and `is_alive` / `exit_code` in every response tell you what happened. `close` SIGKILLs the child (if alive), drains the last bytes into history, and removes the session.

`output` is **destructive read + history gateway**: it drains everything currently sitting in the PTY, appends it to the history ring buffer, and returns it. Two output calls in a row → the second is empty. **The history only contains bytes that some `output` call has already drained** — fresh PTY bytes that no `output` has touched yet are not yet in history.

`output_history` is **non-destructive random access** into that history ring. It uses absolute byte offsets anchored to the lifetime of the *drained* stream (not to the buffer's current contents). When the ring overflows, `total_length` keeps climbing and `buffer_start` advances past the dropped bytes. Read it as many times as you like with no side effect.

**Important consequence — there is no background reader.** If you spawn a chatty child and never call `output`, the PTY kernel buffer (~8 KB) fills up and the child's next `write(stdout)` **blocks**. That's deliberate backpressure: no memory leak, no OOM risk, just a frozen child until you `output` and unblock it. So on long-running jobs, call `output` periodically even if you don't care about the return value — that single act both records to history and keeps the child making progress.

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

Spawn it and call `output` periodically — even if you don't care about the return value, you have to. It both keeps the child unblocked (PTY buffer drained) and pushes the bytes into history so `output_history` can find them later:

```
sid = run("./fuzzer --target ./bin --time 600")
# work on other things, but come back every few turns:
_ = output(sid)                   # drains PTY → history (ignore return if you want)
# …later, look at the whole picture:
hist = output_history(sid)        # total_length, buffer_start, content
```

If the child outpaces the ring buffer (default 1 MiB) between your output calls, the oldest bytes silently drop — `output_history` tells you `total_length` (how much was ever drained) and `buffer_start` (offset of the oldest still-buffered byte). Bump `TERMINAL_MCP_BUFFER_BYTES` at server start, or call `output` more often.

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
- **`output` drains the PTY *and* writes to history.** Calling it twice in a row → the second is empty (PTY was already drained). `output_history` is the non-destructive read of what `output` has already drained — call it as many times as you like.
- **`output_history` only sees what some `output` call has already drained.** Fresh bytes sitting in the PTY kernel buffer haven't been promoted yet. If `output_history` looks empty for a freshly-spawned session, call `output` first.
- **If you never call `output`, the child blocks on its stdout.** The PTY kernel buffer is small (~8 KB). Once it fills, `write(stdout)` in the child blocks — the process *freezes* until you `output` and drain it. This is backpressure, not a bug. On long jobs, call `output` periodically even if you ignore the return value.
- **History silently evicts old bytes.** The ring is bounded (default 1 MiB, set at server start via `TERMINAL_MCP_BUFFER_BYTES`). If `output` drains more than the cap before you next call `output_history`, the oldest bytes are dropped — `total_length` still reports the lifetime drained count, but `output_history(offset=0)` only returns from `buffer_start` onward. Check `buffer_start > 0` to detect loss. Don't assume `offset=0` means "from the beginning of the run" — it means "from whatever's still buffered."
- **Sessions outlive their process.** After the child exits, `is_alive=false` and `exit_code` is set, but the session and its history are still there. Call `close` when you're really done — otherwise dead sessions and ring buffers accumulate.
- **`run` returns before the child has produced any output.** If you `run("echo hi")` and immediately `output`, you may get an empty string. Either call `output` 2–3 times with brief gaps, or check `is_alive` — once it's false you've definitely got everything.
- **Bearer-token isolation.** `list_sessions` only shows your own sessions — you can't see or steal another agent's. If you expect a session to be there and it isn't, you may be talking to the server with a different token than you think.
- **Window size matters for some tools.** Programs like `less`, `vim`, gdb's TUI render based on `$LINES` / `$COLUMNS`. The default is 24×80. If output gets paginated or wraps oddly, `resize(sid, rows, cols)` to something bigger (e.g. 50×200).
- **For control characters** (Ctrl-C, Ctrl-D, Ctrl-Z), send the raw byte: `input(sid, "\x03")` for Ctrl-C, `"\x04"` for EOF. You can also use `send_signal(sid, 2)` for SIGINT to the process group.

## Quick reference

| Tool | Purpose |
|---|---|
| `run(cmd, cwd?, env?, rows?, cols?)` | Spawn `/bin/sh -c <cmd>` in a fresh PTY. Returns `{session_id, pid}`. |
| `input(session_id, data, binary?)` | Write to stdin. `binary=true` → `data` is base64. |
| `output(session_id, binary?)` | Drain PTY, append to history, return drained bytes. Returns `{content, bytes, is_alive, exit_code, exit_signal}`. |
| `output_history(session_id, offset?, length?, binary?)` | Non-destructive random-access read of history (only contains what previous `output` calls drained). Returns `{content, content_offset, bytes, total_length, buffer_start}`. Negative `offset` = from end. `length` null / -1 = until end. |
| `list_sessions()` | Inventory your sessions. |
| `close(session_id)` | SIGKILL, reap, drain final bytes into history, and remove. Use `send_signal` first if you want graceful termination. |
| `send_signal(session_id, sig)` | Send a signal without removing. |
| `resize(session_id, rows, cols)` | Change PTY window size. |
