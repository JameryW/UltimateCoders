# Remove Dead Orchestrator _pending_task_count Field

## Background

`Orchestrator._pending_task_count: int = 0` (orchestrator.py:110) is a
stored int field that is **never read**. The `pending_task_count` property
(orchestrator.py:378-386) computes the value dynamically by iterating
`self.tasks` (counting `IN_PROGRESS` + `PAUSED`). The stored
`_pending_task_count` field is a stale init residue from an earlier
field-counter implementation that was replaced by the dynamic property.

## What I already know (verified this session)

- `orchestrator.py:110` — `self._pending_task_count: int = 0` (only write site).
- `grep -rn "_pending_task_count" python/ tests/` → exactly 1 hit: the
  `:110` assignment. Zero reads anywhere.
- `pending_task_count` (no underscore) is a `@property` at `:378` — it
  returns a dynamic count over `self.tasks.values()`, does NOT read
  `_pending_task_count`. This property is LIVE: read by
  `nats_worker.py:2082`, `dashboard/app.py:634,1260,1267`, and tests
  (`test_workflow_orchestration.py:498-517`).
- No code mutates `_pending_task_count` after init (no `self._pending_task_count +=`
  anywhere). It stays `0` forever.

## The gap

A stored counter field that suggests field-based counting exists when the
actual implementation is a dynamic property. Dead field → delete.

## Decisions (locked)

- **D1**: Remove `self._pending_task_count: int = 0` (orchestrator.py:110).
- **Out of scope**: the `pending_task_count` @property (LIVE); other
  Orchestrator fields; the tasks-counting logic itself.

## Acceptance Criteria

- [ ] `grep -rn "_pending_task_count" python/ tests/` → 0 hits.
- [ ] `pending_task_count` property still works (tests pass).
- [ ] `ruff check` green; `pytest tests/python/` green.

## Technical Approach

1. `orchestrator.py`: delete line 110 `self._pending_task_count: int = 0`.
2. Verify: `ruff check`, `pytest tests/python/test_workflow_orchestration.py`,
   full `pytest tests/python/`.
3. Confirm `pending_task_count` property unaffected (it doesn't read the field).

## Risk

- **None**: field never read, never mutated, primitive int (no `__del__`).
  The property computes dynamically. Deletion cannot change behavior.
