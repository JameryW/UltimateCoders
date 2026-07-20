# dashboard-task-timestamps-zero

## Goal

`_build_snapshot` (nats_worker.py) hardcodes task `created_at`/`updated_at` to `0`, so the gRPC dashboard always shows epoch timestamps. `Task` dataclass (`agent/types.py:244-245`) holds real `datetime` fields — emit them as epoch seconds to match `json_to_task_proto`'s i64 read (dashboard_service.rs:649-650).

## What I already know

* Bug site: `python/ultimate_coders/nats_worker.py:2371` — `"created_at": 0, "updated_at": 0`.
* `Task.created_at`/`Task.updated_at` are `datetime` (default `datetime.now(timezone.utc)`, never None).
* Rust consumer: `json_to_task_proto` reads `as_i64().unwrap_or(0)` — expects Unix epoch seconds.
* Verified no key-name mismatch in full audit (memory: grpc-json-to-key-mismatch-pattern round 40).

## Requirements

* Emit `int(t.created_at.timestamp())` and `int(t.updated_at.timestamp())` in `_build_snapshot` task dict.
* Datetimes are tz-aware (UTC) — `.timestamp()` gives correct epoch.

## Acceptance Criteria

* [ ] Dashboard task list shows real created/updated times, not 1970-01-01.
* [ ] `cargo test -p uc-engine` stays green (no Rust change, but confirm Python path).
* [ ] Python tests green.

## Definition of Done

* Fix applied.
* Tests green (lint/typecheck + relevant suite).
* PR opened + CI green + merged.

## Out of Scope

* Wiring `json_to_subtask_proto` (dead helper — no `_dash_listtasks` responder).
* Scheduler panel wiring (separate feature).

## Technical Notes

* nats_worker.py:2371 (`_build_snapshot` task loop)
* agent/types.py:244-245 (Task datetime fields)
* dashboard_service.rs:649-650 (Rust i64 read)
