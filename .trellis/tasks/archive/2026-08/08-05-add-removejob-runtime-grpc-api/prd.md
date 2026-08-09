# Add RemoveJob runtime gRPC API

## Goal

AddCronJob (#556) added runtime job creation. RemoveJob is the complement — delete a scheduled job via gRPC (currently requires restart). `SchedulerService::remove_job` (service.rs:298) exists + is tested, but there's no proto RPC for it. Add it, mirroring #556's pattern.

## What I already know

* `SchedulerService::remove_job(&self, task_id: &Uuid) -> Result<(), EngineError>` (service.rs:298) — persists removal + deregisters from the job scheduler. Already built (PR2).
* `AddCronJob` proto + RPC (#556) — the pattern to mirror (EngineApi trait method + default impl + LocalEngine delegation + DashboardService routing).
* `EngineApi::add_cron_job` (#556) — the trait method exists; add `remove_job` alongside it.
* No `RemoveJobRequest`/`RemoveJobResponse` proto messages exist yet.

## Implementation (mirror #556)

1. **Proto** (`engine.proto`): add `rpc RemoveJob(RemoveJobRequest) returns (RemoveJobResponse)` + messages. Request: `{string job_id}`. Response: `{bool success, optional string error}`.
2. **EngineApi trait** (`uc-types/src/engine.rs`): add `async fn remove_job(&self, job_id: &str) -> Result<bool, EngineError>` (default impl: `Ok(false)` "Scheduler not available"). Or a `RemoveJobResult` struct mirroring AddCronJobResult.
3. **LocalEngine impl** (`local.rs`): parse job_id → Uuid, call `scheduler_service().remove_job(&uuid)`, convert → result.
4. **DashboardService** (`dashboard_service.rs`): add `remove_job` RPC handler, route to engine.
5. **Feature-gated**: scheduler-off → default impl returns false; no-scheduler build compiles.

## Acceptance Criteria

* [ ] RemoveJob proto RPC + messages
* [ ] EngineApi::remove_job trait method (default false)
* [ ] LocalEngine impl delegates to SchedulerService::remove_job
* [ ] DashboardService routes
* [ ] Feature-gated
* [ ] Tests
* [ ] CI green

## Out of Scope

* UpdateJob RPC (separate — more complex, partial updates)
* Dashboard UI (RPC only)

## Technical Notes

* `service.rs:298` — SchedulerService::remove_job (already built)
* `engine.proto:50,574-588` — AddCronJob proto (the pattern)
* `uc-types/src/engine.rs` — EngineApi::add_cron_job (#556 pattern)
* `dashboard_service.rs:145` — AddCronJob handler (the routing pattern)
