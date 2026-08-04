# Implement AddCronJob runtime API

## Goal

Implement the `AddCronJob` gRPC RPC (currently a UNIMPLEMENTED stub from #547). Allows runtime job creation via the dashboard/API instead of requiring a `uc.scheduler.yaml` edit + gateway restart. The proto + stub already exist (#547); this wires the real implementation.

## What I already know

* **Proto** (`engine.proto:50,574-588`): `AddCronJob(AddCronJobRequest) returns (AddCronJobResponse)`. Request: `{description, cron_expression, project_id, night_window_start?, night_window_end?, timezone, enabled}`. Response: `{success, job_id, error?}`.
* **Stub** (`dashboard_service.rs:145-156`): returns `Status::unimplemented`. Routes to the engine (the DashboardService has engine access per #547).
* **EngineApi trait** (uc-types/engine.rs): has `get_scheduler_status()` + `trigger_scheduler_job()` (from #547, default impls). Does NOT have `add_cron_job()`. Need to add it.
* **SchedulerService** (`uc-engine/src/scheduler/service.rs`): `add_cron_job(task: ScheduledTask) -> Result<AddJobResult, EngineError>` (line 188) — persists + registers. Already built (PR2). The EngineApi method delegates to this.
* **ScheduledTask** (uc-types/scheduler.rs:18): needs `cron_expression`, `night_window_start/end` (NaiveTime), `timezone`, `description`, `project_id`, `enabled`. The proto fields map; `night_window_start/end` are optional strings (HH:MM) → parse to NaiveTime.
* **config.rs** (`SchedulerFileConfig::resolve`) already parses HH:MM → NaiveTime + validates. Reuse `parse_hhmm` (config.rs:157) or duplicate the parse.

## Open Questions

* **EngineApi trait method**: add `add_cron_job(request: AddCronJobRequest-like) -> Result<AddJobResult, EngineError>` with a default impl returning an error (engine without scheduler feature)? Mirror #547's default-impl pattern.
* **Night window**: if `night_window_start`/`end` are absent, use the service-level default (or None = fire anytime)? Recommend: if absent, no per-job window (fire anytime); if present, set the per-job window.
* **Registration timing**: `SchedulerService::add_cron_job` already handles persist + register-if-started. The EngineApi method just builds the ScheduledTask + calls it.
* **DashboardService**: replace the UNIMPLEMENTED stub with `self.engine().add_cron_job(req)` + convert AddJobResult → AddCronJobResponse.

## Requirements (evolving)

* `EngineApi::add_cron_job` trait method (default impl: error "scheduler not available").
* `LocalEngine` impl: build ScheduledTask from the request, call `scheduler_service().add_cron_job(task)`.
* `DashboardService::add_cron_job`: route to engine, convert result → response (success + job_id).
* Night window parse (HH:MM → NaiveTime) reusing config.rs pattern.
* Feature-gated (scheduler feature off → default impl returns error).
* Tests: add_cron_job creates a job (mock scheduler), night-window parse, response conversion.

## Acceptance Criteria

* [ ] EngineApi::add_cron_job trait method + LocalEngine impl
* [ ] DashboardService routes to engine (no longer UNIMPLEMENTED)
* [ ] Night window HH:MM parsed
* [ ] Response {success, job_id} returned
* [ ] Feature-gated (no-scheduler default error)
* [ ] Tests pass
* [ ] CI green

## Out of Scope

* Dashboard UI for job creation (RPC only; UI is a separate frontend task)
* RemoveJob / UpdateJob RPCs (separate)
* Persisting across restarts (SchedulerService already persists via store)

## Technical Notes

* `engine.proto:50,574-588` — AddCronJob proto
* `dashboard_service.rs:145-156` — the UNIMPLEMENTED stub
* `uc-types/src/engine.rs` — EngineApi trait (add get_scheduler_status/trigger_scheduler_job pattern from #547)
* `uc-engine/src/scheduler/service.rs:188` — SchedulerService::add_cron_job
* `uc-types/src/scheduler.rs:18` — ScheduledTask
* `uc-engine/src/scheduler/config.rs:157` — parse_hhmm (reuse)
