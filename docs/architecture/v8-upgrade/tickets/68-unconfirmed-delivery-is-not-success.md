# 68 — A subtask with an unconfirmed publish was reported complete (T68)

**The gate that missed two verdicts.** `WorkspaceManager.release` returns a `push_status`
that grew wings over time: `"failed"` (definite) always existed, then T56/T57 added
`"unknown"` (a publish awaiting reconciliation) and `"busy"` (another process holds the effect
claim). The worker's delivery gate was written before them and only knew `"failed"`:

```python
if result.success and (
    merge_result.get("status") in ("conflict", "commit_failed")
    or merge_result.get("push_status") == "failed"
):
    result.success, result.retryable = False, False
```

For `push_status` `"unknown"` or `"busy"` the condition is false, so the subtask went on to be
recorded **complete with `success=True`** — and the retry that runs reconciliation (the whole
point of T56/T57) never happened. The branch may never have reached the remote, silently: the
exact "partial delivery reported as success" shape the ledger work exists to prevent.

**Fix.** `_delivery_verdict(merge_result) -> (failed, retryable)` makes the mapping explicit and
testable:

| release result | failed | retryable |
|---|---|---|
| `status` conflict / commit_failed | yes | no (reproducing it changes nothing) |
| `push_status` failed | yes | no |
| `push_status` unknown / busy | **yes** | **yes** (the retry reconciles first) |
| pushed / already_pushed / no_changes | no | — |

The worker now uses it; an ambiguous verdict fails the subtask with "Code delivery unconfirmed;
workspace preserved" and keeps it retryable, so the retry settles the publish instead of the
work silently completing.

**Tests** (`tests/python/test_worker_delivery_verdict.py`): a table over every verdict
(`conflict`, `commit_failed`, `failed`, `unknown`, `busy`, `pushed`, `already_pushed`,
`no_changes`, `not_found`, empty), plus an explicit assertion that the ambiguous verdicts are
retryable while the terminal ones are not. The helper is new, so the old code fails these by not
having it; the removed condition in the diff shows the behaviour it replaces (busy/unknown →
`False` → success stayed `True`).

**Status:** implemented (`python/ultimate_coders/agent/worker.py`,
`tests/python/test_worker_delivery_verdict.py`)

**Boundary:** "not_found" (released twice) and "already delivered" stay successes. The change
does not make the *push* path stricter — it makes the worker stop treating "we never confirmed
the publish" as done.
