# 65 — A killed command takes its descendants with it (T65)

**The gap.** `_kill_process_tree` reaps the whole process group on POSIX
(`start_new_session` makes the child a leader, one `killpg` ends the tree) and, on Windows,
did only `proc.kill()` — the direct child. Its docstring called that "the documented
fallback" because "Windows has no process groups".

Two things make that fallback wrong in this repository:

1. the agent CLI **spawns tool subprocesses** — that is the entire reason the helper exists
   ("killing only the direct child orphans the rest of the group");
2. the repo already contains the Windows containment machinery for the *other* path:
   `inference.process.spawn_command` assigns the child to a **job object** on Windows and
   tracks POSIX sessions in a registry. So "no process groups" is true; "no way to kill a
   tree" is not — Windows has `taskkill /T`.

**Consequence of the gap:** after a timeout or a cancellation **without** a cancel file
(the common agent path — `cancel_file` comes from inference runs), the CLI's tool
subprocesses keep running: the worker slot is released while the CPU/GPU work is not.

**Fix.** On Windows, terminate the tree with `taskkill /F /T /PID <pid>` (bounded at 10s,
no console window), falling back to `proc.kill()` if `taskkill` is unavailable or fails.
POSIX behaviour is unchanged.

**Proof.** `tests/python/test_sandbox_process_tree.py` starts a child that spawns a
grandchild which appends to a file every 20 ms — "did it survive" is measured as "did the
file keep growing", so no PID liveness probing is needed. Killed via `_kill_process_tree`:

- **with the fix (Windows, this machine): 1 passed** — the file stops growing;
- **without the fix: 1 failed** — the file kept growing after the parent was killed
  (`_size` 27 > the after-kill size), i.e. the grandchild survived.

Linux CI exercises the POSIX branch of the same test (already group-killed), which is the
no-regression half.

**Status:** implemented (`python/ultimate_coders/agent/sandbox.py`,
`tests/python/test_sandbox_process_tree.py`)

**Boundary:** `taskkill` runs synchronously (≈50 ms in practice, bounded at 10 s) because
the helper is called from both sync and async call sites; a fire-and-forget variant would
need the call sites to become async. Descendants that deliberately detach (a new session on
POSIX, a new job on Windows) still escape — that is what `inference.process`'s registry and
job objects are for.
