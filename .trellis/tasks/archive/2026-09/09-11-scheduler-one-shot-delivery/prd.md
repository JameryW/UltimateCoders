# Scheduler one-shot delivery guarantees + PR-gating Postgres CI

Record of delivered work. Tracker: **PR #628** (squash-merged as `fff4a99`), spec
authority `.trellis/spec/backend/scheduler-spec.md`. Follow-up work split out to
`09-11-ci-storage-integration-serial` and `09-11-cold-start-migration-race`.

## Goal

A one-shot scheduled job gets exactly one attempt. Three paths discarded that
attempt with nothing the operator could see, and the CI around the durable
scheduler could not tell a live database from a dead one.

## Requirements (delivered)

* **Expired during gateway downtime** → recovery appends one `Skipped`
  `ExecutionHistory` stamped with the missed instant; never back-fired.
* **Deferred by the night window** → the guard wrote "next window starts at X"
  while nothing would ever arrive at X; the job is now re-armed at the window
  opening, committing the owed attempt before scheduling it.
* **No worker / NATS down** → bounded backoff retry (60s doubling, 900s cap,
  3 retries) for one-shots, with the budget in a new durable `dispatch_attempts`
  column so a restart cannot launder it back to full.
* Manual triggers must neither reschedule the standing plan nor spend the
  scheduled budget; paused jobs must advertise no run, even after a manual
  trigger.
* PostgreSQL paths must be covered on PRs, and must fail loudly rather than pass
  against an unreachable database.

## Decision (ADR-lite)

* `next_execution` gains meaning for one-shots: **the attempt the scheduler
  owes**, not a derived display value. One field carries deferrals and retries,
  so both survive restarts without a second state machine and without a new
  schema beyond the counter.
* `DispatchSource::{Scheduled, Manual}` — provenance decides whether a failure
  may reschedule anything.
* `Deferred` history does **not** suppress the missed-run record; a deferral that
  is never retried is precisely the loss being surfaced. (Found while writing the
  tests, not before.)
* Re-arm never moves an owed attempt earlier, so deferral and backoff cannot
  pull each other forward.
* Runtime re-registration sits behind a declared `BoxFuture<'static, ()>`:
  `register_one_shot → fired callback → dispatch_with_guard → rearm →
  register_one_shot` is otherwise a type cycle that rustc rejects. The box is
  load-bearing, not style.
* `dispatch_attempts` ships as `ADD COLUMN IF NOT EXISTS … NOT NULL DEFAULT 0`
  following the `verify_command` precedent, so a rolling deploy needs no DDL.
* Retry state is not added to the proto: the retry instant is already `next_run`
  and the attempt count is written into the history `result_summary`.

## Out of Scope

* Concurrent cold-start migration race (`CREATE TYPE`) — separate task.
* `storage-integration` job lacking `--test-threads=1` — separate task.
* Multi-gateway live-read consistency; unifying per-job re-arm with the NATS
  `schedule.window.opened` orchestrator exclusive mode.

## Completion Log (2026-09-11, PR #628)

* Rust: `SchedulerService::set_job_enabled` retained from #627;
  `record_missed_one_shot`, `rearm_one_shot_at`, `rearm_registration_task`,
  `retry_delay_for`, `DispatchOutcome`, `one_shot_attempt_at`;
  `mark_execution_started` now maintains the budget under the existing
  monotonicity guard; `register_one_shot_with_scheduler` schedules the owed
  attempt rather than the raw `execute_after`.
* Contract: `uc_types::ScheduledTask::dispatch_attempts`,
  `scheduler/store.rs` column + explicit i32 narrowing, `migration.rs` ALTER,
  `local.rs` trigger passes `DispatchSource::Manual`.
* Tests: 15 new unit tests (missed record, idempotence across restarts,
  Deferred still records the miss, paused/cron exclusions, ordered cache insert,
  backoff schedule, budget reset/exhaustion, manual inertness, re-armed retry
  re-registered on recovery).
* CI/tests: `schedule_postgres_tests` (legacy ALTER upgrade in a throwaway
  schema; retry-state round trip through save/load/update/list/delete),
  `connected_store()` guard, new `postgres-integration` job running on every PR.
* Verification: fmt 0; `clippy --workspace --all-targets --all-features
  -D warnings` 0; uc-engine 440 passed (+scheduler) / 370 (no-default-features);
  uc-types 23; uc-grpc 172+8; uc-grpc-server 35; actionlint clean; live Postgres
  7/7 in 0.12s and 7/7 loud failures against a dead port.
* Post-merge CI on main: 8/8 jobs success including `postgres integration tests`.

## Known limits (recorded honestly)

* The cold-DB `--test-threads=1` fix covers the new job only; the main-only
  `storage-integration` job still runs the parallel ignored suite and passed by
  luck.
* `--test-threads=1` routes around the product-level migration race; it does not
  remove it.
* The new column's behaviour is verified against a real Postgres by ignored
  tests, which CI runs only in the postgres job.
