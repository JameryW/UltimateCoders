# Add event_emitter.py test coverage

## Goal

`python/ultimate_coders/agent/event_emitter.py` (`TaskEventEmitter` + `TaskEvent` + `dedup_event_key`) is wired into the orchestrator (line 118) + nats_worker (line 1187) but has NO test file. Add test coverage.

## What I already know

* `TaskEvent` dataclass (line 41): `to_dict()` method.
* `dedup_event_key(task_id, subtask_id, event_type)` (line 66): generates a dedup key.
* `TaskEventEmitter` (line 75): `__init__(buffer_size=500)`, `async emit(...)`, `get_recent_events(...)`.
* Used by orchestrator.event_emitter + nats_worker.

## Requirements

* New `tests/python/test_event_emitter.py` covering: TaskEvent.to_dict, dedup_event_key (deterministic + format), emit + get_recent_events (buffer behavior, ordering, max size), async emit.
* pytest + ruff pass.

## Acceptance Criteria

* [ ] test_event_emitter.py created
* [ ] All public API tested
* [ ] pytest + ruff pass
* [ ] CI green

## Technical Notes

* `python/ultimate_coders/agent/event_emitter.py` — the module
* `python/ultimate_coders/agent/orchestrator.py:118` — usage
