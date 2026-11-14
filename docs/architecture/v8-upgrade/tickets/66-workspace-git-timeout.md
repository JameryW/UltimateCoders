# 66 — Workspace git calls are bounded, and a timed-out push is ambiguous (T66)

**The gap.** `WorkspaceManager._git` awaited `proc.communicate()` with **no timeout**, and it
drives every workspace operation:

| call site | git operation | hang source |
|---|---|---|
| `acquire` | `fetch`, `worktree add` | DNS blackhole, dead SSH |
| `release` | `status`, `commit`, `merge`, `worktree remove` | index.lock, file locks |
| `_push_subtask_branch` | `push` | credential prompt, dead SSH |
| `_settle_branch_push` | `ls-remote` | DNS blackhole |

A hung call blocks the caller **while the worktree is still held**: `cleanup()` cannot help
because the handle is still active. This is the unbounded-call class T50 fixed for
`subprocess.run` in `inference/`, in the `asyncio.create_subprocess_exec` form this module
uses — T50's guard (`scripts/check-subprocess-timeouts.py`) cannot see it, and the module was
missed.

**The second half, which matters more.** A timed-out **push** is not a definite failure: the
remote may have applied it. The old code classified the failure only by looking at git's
stderr (`_ambiguous_push_failure`), and a timeout message matches no marker there — so the
receipt was written `FAILED`, which authorises a retry of a publish that may already have
landed. That is exactly what the ledger exists to prevent.

**Fix.**

1. `_git` is bounded by `GIT_TIMEOUT_SECONDS = 120`; on timeout it kills the child, waits up
   to 10 s for it to settle, logs the operation, and returns
   `{"exit_code": -1, "timed_out": True, "stderr": "git <op> timed out after 120s"}`.
2. `_push_subtask_branch` treats `timed_out` as **ambiguous** regardless of stderr: the
   receipt becomes `UNKNOWN` and the existing reconcile path decides (`ls-remote`), so a
   retry only happens when the remote is proven not to have the commit.

**Tests** (`tests/python/test_workspace_git_timeout.py`):

- `a_hung_git_call_is_bounded_and_killed` — a stub process whose `communicate()` never
  returns: `_git` returns `exit_code=-1`, `timed_out=True`, the operation in the message, and
  the child **was killed**.
- `a_timed_out_push_is_reconciled_not_failed` — a timed-out push with an unreachable remote
  reports `push_status="unknown"` and leaves the receipt `UNKNOWN`. **Fails on the unfixed
  file** (`failed` instead of `unknown`), which is the retry-authorising bug.

**Status:** implemented (`python/ultimate_coders/agent/workspace.py`,
`tests/python/test_workspace_git_timeout.py`)

**Boundary:** the bound is a fixed 120 s, matching T50's for inference git; a legitimate
operation that needs longer needs a decision, not a silent raise. The timeout is not
propagated as an exception, so every existing caller keeps its current shape (a non-zero exit
it already handles).
