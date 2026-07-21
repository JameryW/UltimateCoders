# nats-worker-stop-subtaskstatus-running-attrerror

## Goal

`NatsWorker.stop()` (`python/ultimate_coders/nats_worker.py:566`) references
`SubtaskStatus.RUNNING` to detect in-flight subtasks during shutdown:
```python
st.status in (SubtaskStatus.RUNNING, SubtaskStatus.ASSIGNED)
```
But `SubtaskStatus` (agent/types.py:24) has NO `RUNNING` member — only
PENDING/ASSIGNED/IN_PROGRESS/COMPLETED/FAILED/CONFLICTED. So accessing
`SubtaskStatus.RUNNING` raises `AttributeError` on the first non-ASSIGNED
in-flight subtask, aborting the shutdown reporting loop. All remaining
abandoned subtasks stay IN_PROGRESS forever and the task never reaches
terminal.

## What I already know

- Bug site: `nats_worker.py:566`.
- Enum `SubtaskStatus` (agent/types.py:24-31): no RUNNING. The in-flight
  member is `IN_PROGRESS`.
- Fix: replace `SubtaskStatus.RUNNING` with `SubtaskStatus.IN_PROGRESS`.

## Requirements

- Use the correct enum member `IN_PROGRESS`.

## Acceptance Criteria

- [ ] `SubtaskStatus.RUNNING` → `SubtaskStatus.IN_PROGRESS`.
- [ ] `ruff check` clean.
- [ ] Existing python tests green; add a regression test asserting stop()
      reports IN_PROGRESS subtasks (no AttributeError).

## Definition of Done

- Fix + regression test.
- PR opened + CI green + merged.

## Out of Scope

- Other Python scan findings (nats_worker:1885 wrong-nesting, race conditions,
  aggregator base drift, etc.) — separate tasks.

## Technical Notes

- `python/ultimate_coders/nats_worker.py:564-568` (shutdown reporting loop).
- `python/ultimate_coders/agent/types.py:24-31` (SubtaskStatus enum).
