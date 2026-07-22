# log-bg-task-exceptions-spawn-bg

## Goal

`_spawn_bg` (`nats_worker.py:1060`) schedules background tasks with `add_done_callback(self._bg_tasks.discard)` — only discards the strong ref, never retrieves or logs the exception. If a wrapped coro raises (`_execute_subtasks` dispatch, `_execute_and_report` remote subtask execution), the error surfaces only as asyncio's default "Task exception was never retrieved" — no context-aware log. The task dies silently, the subtask stalls IN_PROGRESS until the 90s heartbeat-stall reaper kicks in. Fix: the done-callback retrieves and logs the exception at warning with `exc_info`.

## What I already know

* `_spawn_bg` at `nats_worker.py:1060-1070`: `task = asyncio.create_task(coro)`, `self._bg_tasks.add(task)`, `task.add_done_callback(self._bg_tasks.discard)`, return task.
* 2 callers: `_execute_subtasks` (line 1145, orchestrator local batch), `_execute_and_report` (line 1661, remote subtask execution).
* Same anti-pattern also at `worker.py:1879` (`_broadcast_memory_changed`) and `orchestrator.py:304` (`_schedule_arbitration`).
  - `worker.py:1879`: low real impact — `publish_memory_changed` → `_publish` already catches all exceptions internally (`_publish` line ~296 `except Exception: logger.warning(...)`), so the task never propagates an exception. Skipping (not in scope; would be dead-code hardening).
  - `orchestrator.py:304`: merge arbitration, explicitly non-fatal by design ("arbitration failures are logged, never crash"). Out of scope.
* Spec: `logging-guidelines.md` — `warning` for non-critical failures, `exc_info=True`, `%s` formatting (no f-strings), module logger `logger = logging.getLogger(__name__)` (already present).
* asyncio: `task.exception()` raises `InvalidStateError` if task not done — safe to call only inside done-callback (task IS done there). `task.cancelled()` → True if cancelled, then `.exception()` raises `CancelledError`; must check `.cancelled()` first.

## Requirements

* `_spawn_bg` done-callback: discard the ref (current behavior) AND retrieve + log the exception at `warning` with `exc_info`.
* Cancelled tasks: skip (expected on shutdown) — log nothing, no `CancelledError` noise.
* No behavior change to task scheduling/return value.

## Acceptance Criteria

* [ ] Task whose coro raises → done-callback logs warning with the exception traceback.
* [ ] Cancelled task → no log.
* [ ] Successful task → no log, ref discarded.
* [ ] Unit test: spawn a coro that raises → assert warning logged + ref removed from `_bg_tasks`.

## Definition of Done

* Tests added/updated.
* ruff clean, CI green.
* Behavior noted in commit message.

## Technical Approach

Replace the inline `add_done_callback(self._bg_tasks.discard)` with a bound method `_on_bg_done`:

```python
def _on_bg_done(self, task: asyncio.Task[Any]) -> None:
    self._bg_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Background task failed", exc_info=exc)
```
`task.add_done_callback(self._on_bg_done)`.

## Decision (ADR-lite)

**Context**: bg tasks dying silently hide execution failures until heartbeat-stall reaper.
**Decision**: single done-callback on `_spawn_bg` logs exceptions; covers both callers. Worker/orchestrator variants left alone (low-impact dead-code / intentional-non-fatal).
**Consequences**: operators see execution failures immediately instead of after 90s stall. One centralized fix.

## Out of Scope

* `worker.py:_broadcast_memory_changed` callback (low-impact; `_publish` already swallows).
* `orchestrator.py:_schedule_arbitration` callback (intentional non-fatal).
* Changing which coros run as bg tasks.

## Technical Notes

* File: `python/ultimate_coders/nats_worker.py` ~lines 1060-1070.
* Test: `tests/python/test_nats_worker_helpers.py` (follow existing `_make_worker` pattern).
