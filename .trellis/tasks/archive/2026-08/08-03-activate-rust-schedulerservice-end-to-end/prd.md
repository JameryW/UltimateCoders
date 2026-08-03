# Activate Rust SchedulerService end-to-end

## Goal

Make the scheduler actually fire: cron jobs, one-shot tasks, and night-window guards execute real work instead of the dashboard showing "unavailable". The Rust `SchedulerService` (`crates/uc-engine/src/scheduler/service.rs`) is a complete, tested implementation that is currently orphaned — no caller in the gRPC server, no `EngineApi` trait methods, no Python wiring.

## What I already know

* **Python side**: `orchestrator.py:112` `self.scheduler = None` (stub). `nats_worker._dash_getschedulerstatus` responds to the dashboard RPC via attribute access on `orch.scheduler` → AttributeError when None → dashboard shows "unavailable".
* **Rust side**: `SchedulerService` struct exists in `uc-engine/src/scheduler/service.rs`, fully tested (cron/one-shot/night-window/execution-history). Uses `tokio_cron_scheduler` (feature-gated `scheduler`). `ScheduleDispatcher` trait is a no-op `LoggingDispatcher` (test stub).
* **gRPC/proto**: `GetSchedulerStatus`, `TriggerSchedulerJob` RPCs exist in `engine.proto`. `DashboardService` (`dashboard_service.rs:100`) routes them via `nats_dashboard_request` to the Python responder — NOT to the Rust SchedulerService.
* **EngineApi trait** (`uc-types/src/engine.rs`): has NO scheduler methods. LocalEngine does not expose scheduler.
* **Dashboard**: `SchedulerPanel` + `json_to_scheduler_status_response` already consume the proto (memo: aligned, no key drift). Panel renders fine when data is present.
* **Core NATS vs JetStream** (out-of-scope here, separate gap): `uc.subtask.execute` is core NATS, no redelivery.

## The gap (architectural)

Two implementations exist:
1. Rust `SchedulerService` (tested, dormant)
2. Python `orch.scheduler` (None stub, referenced by NATS responder)

The dashboard RPC currently routes to the Python responder. To activate, EITHER:
- **(A)** Wire Rust `SchedulerService` into the `EngineApi` trait + gRPC `DashboardService` (bypass NATS), making Rust the source of truth
- **(B)** Implement the Python `orch.scheduler` (instantiate a real scheduler), keeping the NATS responder path
- **(C)** Hybrid — Rust stores/manages jobs, Python executes (via NATS dispatch when a cron fires)

## Assumptions (temporary)

* The scheduler is single-gateway (no multi-instance scheduler coordination needed for MVP)
* `tokio_cron_scheduler` is acceptable as the cron engine (already a dep, feature-gated)
* Dashboard/Python don't need to CREATE jobs via gRPC yet (proto only has Get + Trigger) — job creation is config-time

## Decision (ADR-lite)

**Context**: Two scheduler implementations exist — Rust `SchedulerService` (complete, tested, dormant) and Python `orch.scheduler` (None stub, NATS-responder shows "unavailable"). Dashboard RPC routes via NATS to Python.
**Decision**: **Rust owns the scheduler.** Wire `SchedulerService` into the `EngineApi` trait + gRPC `DashboardService` (bypass the NATS responder for scheduler RPCs). Rust owns job storage, night-window, cron-firing. Remove the Python scheduler stub.
**Consequences**: Single source of truth (Rust gateway, already the execution engine). Removes duplicated Python cron logic. DashboardService `GetSchedulerStatus`/`TriggerSchedulerJob` route directly to the engine, not NATS. A cron fire calls back into the engine to dispatch a real subtask (TBD: what "dispatch" means).

## Open Questions

(none blocking — see Decision + Requirements)

## Requirements (evolving)

* New `uc.scheduler.yaml` config (cron jobs + night window), loaded at gateway startup (mirrors `uc.repos.yaml` pattern). Declarative, version-controlled. Add/remove jobs = edit + restart.
* `GetSchedulerStatus` returns `available: true` with real jobs + night-window + execution history (routed to Rust engine, not NATS)
* `EngineApi` trait gains scheduler methods (or a sub-trait) so gRPC `DashboardService` can delegate directly
* `DashboardService::get_scheduler_status`/`trigger_scheduler_job` stop routing via NATS; query the Rust engine instead
* Cron jobs fire on schedule (night-window guard respected); a fire calls `engine.submit_task(scheduled_task.description)` — real work (decompose + dispatch subtasks)
* A real `ScheduleDispatcher` impl wraps the engine (replaces the no-op `LoggingDispatcher`)
* `TriggerSchedulerJob` manually fires a job (same submit path)
* Dashboard SchedulerPanel shows live status (no longer "unavailable")
* Python `orch.scheduler` stub removed (or marked dead)

## Acceptance Criteria (evolving)

* [ ] `uc.scheduler.yaml` parses (cron jobs + night window); loaded at startup
* [ ] `GetSchedulerStatus` returns `available: true` with real jobs + night-window + execution history
* [ ] `DashboardService` routes scheduler RPCs to the Rust engine (not NATS)
* [ ] A configured cron job fires inside the night window → `engine.submit_task` → real subtask dispatch
* [ ] Night-window guard defers jobs outside the window (Deferred ExecutionHistory)
* [ ] Cron-fire FAILURE (submit_task returns Err) records a Failed ExecutionHistory (not just success)
* [ ] `TriggerSchedulerJob` manually fires a job
* [ ] Dashboard SchedulerPanel renders real data (not "unavailable")
* [ ] `AddCronJob` RPC stub present (returns UNIMPLEMENTED) — proto reserved for future runtime creation
* [ ] Python `orch.scheduler` stub removed/dead
* [ ] Tests: config parse, cron-fire dispatch, night-window defer, failure recording
* [ ] CI green (cargo + pytest + bun)

## Definition of Done

* Tests added (Rust unit + integration for the wired path)
* Lint/typecheck/CI green
* Docs/CLAUDE.md updated if the architecture doc changes
* Rollout: default-off (opt-in via config), no behavior change for existing deploys

## Out of Scope (explicit)

* Multi-instance scheduler coordination (distributed cron locking) — single-gateway MVP
* JetStream subtask redelivery (separate gap)
* ResultAggregator activation (separate dormant feature)
* Dashboard UI for runtime job creation (AddCronJob RPC is a stub; UI deferred)

## Technical Approach

**Architecture**: Rust `SchedulerService` becomes the single source of truth. `EngineApi` trait gains scheduler methods; `LocalEngine` delegates to its `SchedulerService`. `DashboardService` scheduler RPCs route to the engine directly (drop the NATS round-trip). Cron-fires call `engine.submit_task(description)` via a real `ScheduleDispatcher` impl. Jobs + night-window declared in `uc.scheduler.yaml`, loaded at startup.

**Implementation Plan (small PRs):**

* **PR1** — Config + trait scaffolding: `uc.scheduler.yaml` schema + parser (Rust), `EngineApi` scheduler methods (`get_scheduler_status`, `trigger_scheduler_job`, `add_cron_job` stub), `LocalEngine` wiring to `SchedulerService`. Tests: config parse, trait delegation. No behavior change yet (scheduler still not started).
* **PR2** — Real dispatch + cron-fire: `EngineSubmitDispatcher` (impl `ScheduleDispatcher`, calls `engine.submit_task`). `SchedulerService::start()` invoked at gateway boot with the loaded jobs + night-window. Cron-fire → submit_task → ExecutionHistory (Completed on success, Failed on submit_task Err). Tests: cron-fire dispatch, failure recording.
* **PR3** — DashboardService routing + night-window + cleanup: `DashboardService` scheduler RPCs route to the engine (not NATS). Night-window defer records Deferred ExecutionHistory. `TriggerSchedulerJob` uses the engine path. Remove/dead-mark Python `orch.scheduler` stub. Dashboard SchedulerPanel renders real data. Tests: night-window defer, trigger, end-to-end status. `AddCronJob` proto stub (UNIMPLEMENTED). Docs: CLAUDE.md scheduler note.

## Technical Notes

* `crates/uc-engine/src/scheduler/service.rs:75` SchedulerService
* `crates/uc-engine/src/scheduler/service.rs:32` ScheduleDispatcher trait (no-op LoggingDispatcher)
* `crates/uc-grpc/src/dashboard_service.rs:100` get_scheduler_status (NATS-routed)
* `python/ultimate_coders/agent/orchestrator.py:112` scheduler=None stub
* `crates/uc-grpc/proto/engine.proto:516` scheduler proto messages
* [[grpc-json-to-key-mismatch-pattern]] memo: scheduler panel shows "unavailable"
