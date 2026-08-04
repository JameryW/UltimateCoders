# Fix scheduler dispatch: route cron-fire through NATS submit

## Goal

Fix the integration gap found in the 2026-08-04 cross-feature audit: the scheduler's cron-fire (`EngineSubmitDispatcher`) calls Rust `engine.submit_task`, which only `insert`s a task into the Rust in-memory store (no decomposition, no subtask dispatch, no NATS publish). Scheduler-created tasks never reach the Python orchestrator → JetStream subtask delivery never fires for them. The PRD's "real work (decompose + dispatch subtasks)" assumption was incorrect for the Rust engine path.

Route the cron-fire through `uc.task.submit` NATS subject (the Python orchestrator's consume path) so scheduler-created tasks enter the normal decompose → JetStream dispatch → worker execution chain.

## What I already know

* `EngineSubmitDispatcher` (`uc-engine/src/scheduler/dispatcher.rs`) calls `engine.submit_task(description, project_id)`.
* `LocalEngine::submit_task` (local.rs:905) → `TaskStore::submit_task` → `InMemoryTaskBackend::submit_task` (task_store.rs:71) = `insert` only. No decomposition.
* Python orchestrator consumes `uc.task.submit` NATS subject → decomposes → dispatches subtasks via JetStream.
* `GrpcServer` (`uc-grpc/src/server.rs:1329`) has `nats_client: Option<async_nats::Client>` (the `with_nats` path connects).
* `SchedulerService` has `set_dispatcher()` (PR2 late-binding) — can swap the dispatcher post-construction.
* `uc-grpc-server/src/main.rs:148` calls `engine.init_scheduler_dispatcher()` (builds `EngineSubmitDispatcher` from `self.clone()`). The gateway has NATS access here but doesn't pass it to the dispatcher.
* `uc-engine` does NOT depend on `async-nats` (lower layer; adding it is a layer violation).

## The gap

`EngineSubmitDispatcher` is the wrong abstraction — it calls Rust `submit_task` (insert-only). The real decompose+dispatch path is NATS → Python. The dispatcher needs to publish to `uc.task.submit`, but `uc-engine` (where the dispatcher lives) can't access NATS without a layer violation.

## Open Questions

* **Where to build the NATS dispatcher?** In `uc-grpc-server` (gateway layer, has NATS) as a new `NatsSubmitDispatcher` impl, injected via `set_dispatcher` (PR2 already supports this). OR in `uc-grpc` (server crate, has the nats_client)? Recommend `uc-grpc` — it's the server crate, has NATS, and `EngineSubmitDispatcher` can stay in `uc-engine` as the no-NATS fallback.
* **Fallback when no NATS**: if `nats_client` is None (no UC_NATS_URL), fall back to `EngineSubmitDispatcher` (current engine.submit_task path) — graceful degradation. OR skip dispatch entirely (scheduler idle)?
* **Published payload**: must match what the Python `uc.task.submit` consumer expects (task_id, description, project_id). Check the existing Python `_handle_submit` responder + the Rust publisher (server.rs has NATS_SUBJECT_TASK_SUBMIT — find what it publishes).
* **Scheduler firing still records ExecutionHistory**: the dispatcher's Ok/Err maps to Completed/Failed history (PR2). The NATS publish is fire-and-forget (publish doesn't wait for decompose). Does the history's "Completed" mean "published" (spawn-equivalent) or "decomposed"? Recommend "published" — matches the fire-and-forget semantics.

## Requirements (evolving)

* New `NatsSubmitDispatcher` (in `uc-grpc`, the server crate with NATS access): impl `ScheduleDispatcher::dispatch` by publishing to `uc.task.submit`.
* Gateway (`uc-grpc-server` main) injects `NatsSubmitDispatcher` via `set_dispatcher` (replacing `EngineSubmitDispatcher` when NATS is connected; fall back to `EngineSubmitDispatcher` when no NATS).
* Published payload matches the Python `uc.task.submit` consumer contract.
* Scheduler cron-fire → NATS publish → Python decompose → JetStream subtask dispatch (the full chain).
* ExecutionHistory still recorded (Completed on publish-success, Failed on publish-error).
* `EngineSubmitDispatcher` retained as the no-NATS fallback (existing tests stay valid).
* Tests: NatsSubmitDispatcher publishes correct payload; fallback to EngineSubmitDispatcher when no NATS; history recording.

## Acceptance Criteria

* [ ] `NatsSubmitDispatcher` impl in `uc-grpc` publishes to `uc.task.submit`
* [ ] Gateway injects it via `set_dispatcher` when NATS connected
* [ ] Fallback to `EngineSubmitDispatcher` when no NATS (no regression)
* [ ] Published payload matches Python consumer (verified against the existing publisher)
* [ ] ExecutionHistory recorded on publish success/failure
* [ ] Tests pass (NatsSubmitDispatcher, fallback, payload)
* [ ] CI green

## Definition of Done

* Tests added
* Lint/CI green
* [[scheduler-activation-feature-2026-08-03]] memo updated (the gap → fixed)

## Out of Scope

* Waiting for decompose completion (fire-and-forget is correct — matches EngineSubmitDispatcher's spawn semantics)
* Multi-instance scheduler locking

## Technical Notes

* `uc-engine/src/scheduler/dispatcher.rs` — `EngineSubmitDispatcher` (current, retained as fallback)
* `uc-grpc/src/server.rs:1329` — `nats_client` field
* `uc-grpc/src/server.rs:36` — `NATS_SUBJECT_TASK_SUBMIT`
* `uc-grpc-server/src/main.rs:148` — `init_scheduler_dispatcher` (injection point)
* `uc-engine/src/local.rs:475` — `init_scheduler_dispatcher` + `set_dispatcher`
* Python `_handle_submit` (`nats_worker.py`) — the consumer contract to match
* [[scheduler-activation-feature-2026-08-03]] — flagged the gap
