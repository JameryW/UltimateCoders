# 64 — A chatty sandbox command was unbounded (T64)

**The asymmetry.** `SandboxManager._execute_subprocess` honours the caller's `timeout_secs`
on one path and not the other:

| path | how the budget is applied |
|---|---|
| no `on_stdout_line` | `asyncio.wait_for(proc.communicate(), timeout=timeout_secs)` — the **whole command** |
| with `on_stdout_line` | `asyncio.wait_for(proc.stdout.readline(), timeout=timeout_secs)` — **per line** |

On the streaming path the clock restarted on every stdout line, so `timeout_secs` was an
*idle* timeout: a command that keeps printing **never** hit it. That is precisely the shape
of a coding-agent CLI printing progress, and the streaming path exists because of that —
so the streaming case was the one most likely to be unbounded.

**Measured, not assumed.** A script printing 40 lines with 50 ms between them, submitted with
`timeout_secs=0.4`, returns `timed_out=False` and `duration_ms=2074` on the unfixed code.

**Consequence.** The sandbox-level budget is what bounds a runaway command; without it the
worker slot (and the workspace the command holds) stays occupied until some *outer* budget
cancels the coroutine — a different, coarser layer whose `CancelledError` path had to be
written separately (see the `finally` block's comment about orphaned coding-agent
processes).

**Fix.** The streaming loop keeps a single deadline (`start + timeout_secs`) and waits
`min(remaining, …)` per read; when the deadline passes it raises the same `TimeoutError` the
existing handler already turns into `timed_out=True` with the process tree terminated.

**Tests** (`tests/python/test_sandbox_stream_deadline.py`):

- `a_chatty_command_still_hits_the_total_timeout` — the 0.4 s budget stops the 2 s chatty
  script (`timed_out=True`, `duration_ms < 1500`, lines streamed before the deadline are
  kept, stopped mid-stream). Fails on the unfixed file.
- `a_quiet_command_completes_normally` — a fast streaming command still exits 0 with its
  streamed line (no regression to the boundary logic).

**Status:** implemented (`python/ultimate_coders/agent/sandbox.py`,
`tests/python/test_sandbox_stream_deadline.py`)

**Boundary:** the streaming budget now matches the non-streaming one exactly; per-line
progress is still emitted up to the deadline. A command that legitimately needs longer still
needs a larger `timeout_secs` — the fix removes the *unbounded* case, it does not lengthen
the default.
