# Scheduler job pause/resume

Tracker: spec #625 → ticket #626 (`feat/scheduler-pause-resume`).

## Goal

Give the scheduler a **reversible stop**. Before this change a job could only be
created, fired, or deleted: `ScheduledTask.enabled` was settable at creation time
only, and "pause for a while" therefore meant `RemoveJob` — which cascades the
job's `execution_history` away in the durable store.

A hidden second defect sat behind it: `SchedulerService::start()` recovered with
`list_tasks(true)`, so a disabled job disappeared from the registry on the next
gateway restart — no longer listed, and impossible to activate or clean up.

## Decision (ADR-lite)

- **Pause clears `next_execution`; resume recomputes from now.** A paused job
  that still advertised a run would lie to the dashboard, and reusing the
  pre-pause timestamp would surface a stale value the previous process computed.
- **Manual trigger of a paused job stays legal**, but `mark_execution_started`
  re-applies the "paused ⇒ no next run" rule, so an operator action cannot
  resurrect the promise.
- **Runtime registration happens before the visible snapshot is committed**, so
  a resume whose registration fails leaves the job untouched instead of
  "enabled but never firing".
- **Recovery loads every persisted task and registers only enabled ones.** This
  is what makes a pause survive a restart *and stay resumable*; it also changes
  the documented meaning of `list_jobs()` / `SchedulerStatus.jobs` from "active
  snapshot" to "all known jobs, paused included".
- **Persistence stays best-effort** (`update_task` failure logs a warning),
  matching this file's existing dispatch-metadata contract rather than inventing
  a second one.
- Pause vs remove are distinct verbs on purpose: remove is destructive and
  remains the only one that discards history.

## Requirements

* Rust: `SchedulerService::set_job_enabled`, shared
  `register_runtime_job`/`unregister_runtime_job` helpers (remove and pause can
  no longer drift), restart recovery change, `mark_execution_started` guard.
* Contract: `EngineApi::set_scheduler_job_enabled` +
  `SchedulerJobEnabledResult`, gRPC `DashboardService::SetSchedulerJobEnabled`,
  TS stubs regenerated for both targets.
* Surfaces: `uc_scheduler` `pause`/`resume` actions,
  `/uc schedule pause|resume <job-id>`, dashboard hook + SchedulerPanel
  Pause/Resume, listings label a disabled job `paused` rather than `off`.

## Out of Scope

* Multi-gateway live-read consistency (unchanged: one-shot startup load).
* Mounting SchedulerPanel in the live dashboard — `onTriggerJob` is likewise
  unwired today, so the button is component parity, not a visible affordance yet.
* The stale Python `agent/scheduler.py` API section in the spec (class removed
  in #548) — separate doc hygiene.

## Completion Log (2026-09-07)

* Rust: `set_job_enabled` (+8 unit tests incl. pause-survives-restart, expired
  one-shot, idempotent toggle); `start()` now `list_tasks(false)` and registers
  only enabled tasks; `mark_execution_started` keeps `next_execution = None`
  while paused; `remove_job` and pause share `unregister_runtime_job`.
* Contract: `SchedulerJobEnabledResult` + trait default impl ("Scheduler not
  available"), `LocalEngine` passthrough, proto
  `SetSchedulerJobEnabled{Request,Response}` + DashboardService handler,
  wire-projection test (`scheduler_status_proto_reports_paused_job_without_next_run`).
* TS: `engine_pb.ts` regenerated for dashboard + orchestrator (npx @bufbuild/buf
  1.72 + local `protoc-gen-es`); `GrpcBridge.setSchedulerJobEnabled`;
  `uc_scheduler` pause/resume; `/uc schedule pause|resume`;
  `useDashboardGrpc().setSchedulerJobEnabled`; SchedulerPanel Pause/Resume +
  PAUSED chip.
* Spec: `.trellis/spec/backend/scheduler-spec.md` — Pause/Resume Semantics table,
  error matrix rows, restart-recovery contract, 8 new test rows.
* Verification: `cargo fmt --all -- --check` 0; `cargo clippy -D warnings`
  (uc-types, uc-engine+scheduler, uc-grpc, `--all-targets`) 0; `cargo test -p
  uc-engine --features scheduler` 423 passed / 0 failed; `cargo test -p uc-grpc`
  153 + 8 passed, `--features messaging` 172 + 8 passed; `cargo test -p
  uc-grpc-server` 35 passed; orchestrator type gate "src is type-clean" (18
  tolerated vendored diagnostics); dashboard `tsc -b && vite build` 0.
