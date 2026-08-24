# Activate Scheduler Postgres Schedule Persistence

## Goal

The Rust engine's `SchedulerService` (scheduler/service.rs) has a **complete schedule-persistence subsystem** — `ScheduleStore` trait, `PostgresScheduleStore` (store.rs:157, feature-gated `storage`), DB migrations (migration.rs: `scheduled_tasks` + `execution_history` tables), write-path persistence (`save_task`/`delete_task`/`save_execution` called from service), AND restart-recovery (`start()` calls `store.list_tasks(true)` then re-registers cron + one-shot jobs).

But it is **never activated in production**. `LocalEngine::new()` hardcodes `SchedulerService::new()` (local.rs:155/221/280), which constructs `InMemoryScheduleStore`. The migrations (`scheduler::migration::run_migrations`) are never called. Result: scheduled tasks silently lost on restart — same dormant-data-loss class as `UC_TASK_BACKEND=postgres` was before PR #576, and `UC_EVENT_BACKEND=nats` before PR #579.

**Activate it**: env-gated `UC_SCHEDULE_BACKEND=postgres` (+ `UC_DATABASE_URL`) constructs `PostgresScheduleStore`, runs migrations, injects into the engine's scheduler. Falls back to in-memory on missing URL / connection failure / storage feature disabled. Mirrors the `UC_TASK_BACKEND` / `UC_EVENT_BACKEND` pattern exactly.

## What I already know (from investigation)

* `ScheduleStore` trait (store.rs:16) — fully specified: `save_task`, `load_task`, `list_tasks`, `update_task`, `delete_task`, `save_execution`, `list_executions`.
* `PostgresScheduleStore` (store.rs:157, `#[cfg(feature = "storage")]`) — fully implemented (INSERT/SELECT/UPDATE/DELETE for both tables).
* `scheduler::migration::run_migrations(pool)` (migration.rs:18) — creates `scheduled_tasks` + `execution_history` + 4 indexes. **Never called in production** (only the `#[cfg(not(storage))]` no-op exists).
* Write path already wired: `add_cron_job`/`add_one_shot_job` → `self.store.save_task` (service.rs:231/270); `remove_job` → `delete_task` (service.rs:300); `record_execution` → `save_execution` (service.rs:453).
* Recovery path already wired: `SchedulerService::start()` (service.rs:487) → `store.list_tasks(true)` → re-registers each persisted task (`register_cron_with_scheduler` / `register_one_shot_with_scheduler`). Persists enabled tasks into `job_metadata`.
* `SchedulerService::with_store(store)` (service.rs:154) and `with_store_and_dispatcher` (service.rs:128) exist — accept an injected store. **But `LocalEngine` never uses them** — always `SchedulerService::new()` (in-memory).
* `LocalEngine` owns `scheduler_service: Arc<SchedulerService>` (local.rs:64). Accessor `scheduler_service()` returns `&Arc<SchedulerService>` (local.rs:452). No `set_scheduler_store` — store is `Arc<dyn ScheduleStore>` (not behind a RwLock), so no late-binding setter exists (unlike `set_dispatcher`/`set_lock_provider` which ARE RwLock-wrapped).
* `main.rs` `create_task_backend()` (main.rs:48) is the precedent: reads `UC_TASK_BACKEND` env, builds `PostgresTaskBackend` from `UC_DATABASE_URL`, returns it; `GrpcServer::with_backends(engine, task_backend, event_store)` threads it in. Scheduler has no equivalent — the engine builds its own scheduler internally.
* `start_scheduler()` (main.rs:207) runs AFTER `GrpcServer` construction; it does late-binding of dispatcher + lock_provider via the accessors. This is the natural injection point for the store too — but the store can't be late-bound without wrapping it in RwLock.

## Assumptions (temporary)

* Same env var naming convention: `UC_SCHEDULE_BACKEND` (`postgres` | `memory` default), reusing `UC_DATABASE_URL` (no separate `UC_SCHEDULE_DATABASE_URL`).
* Migration runs once at startup (idempotent `CREATE TABLE IF NOT EXISTS`), before `start_scheduler`.
* PG pool: build a dedicated `PgPool` for the scheduler (or reuse). task_backend builds its own pool inside `PostgresTaskBackend::new(&db_url)`; metadata_store builds its own. No shared pool today. **Reuse the pattern: scheduler builds its own pool** (matches existing isolation; avoids coupling).
* Fallback semantics match task_backend: missing `UC_DATABASE_URL` / connection failure / storage feature off → warn + in-memory (no crash).

## Open Questions

* ~~Q1 resolved (see Decision)~~ — Approach A chosen.

## Requirements (evolving)

* `UC_SCHEDULE_BACKEND=postgres` + `UC_DATABASE_URL` → `PostgresScheduleStore` constructed, migrations run, scheduler uses PG. Survives restart.
* Default (unset / `memory` / missing URL / connection failure / `storage` feature off) → in-memory, no behavior change for existing deploys.
* `scheduler::migration::run_migrations` called at startup when PG backend selected.
* Restart recovery (already in `start()`) now reads from PG instead of empty in-memory store → cron + one-shot jobs restored.
* No new abstraction, no new trait — reuse existing `ScheduleStore` / `PostgresScheduleStore`.

## Acceptance Criteria (evolving)

* [ ] With `UC_SCHEDULE_BACKEND=postgres` + `UC_DATABASE_URL`, a scheduled cron job survives a server restart (re-registered by `start()` recovery).
* [ ] Without those env vars, scheduler behaves exactly as today (in-memory, no crash, warn on misconfig).
* [ ] `scheduled_tasks` + `execution_history` tables created on first PG startup; idempotent on subsequent.
* [ ] `cargo check` + `cargo test -p uc-engine` green; storage-feature path compiles.
* [ ] No new dependencies; reuses existing sqlx pool pattern.

## Definition of Done

* Tests added (unit for env-gating logic where feasible; the recovery path is already tested in service.rs).
* `cargo check` (default + `storage`/`scheduler` features) + `cargo test -p uc-engine` green.
* Spec note (checkpoint-spec or new scheduler-spec entry) documents `UC_SCHEDULE_BACKEND`.
* Rollout: opt-in via env var, zero behavior change when unset (safe default).

## Decision (ADR-lite)

**Context**: `SchedulerService::start()` reads `store.list_tasks(true)` for restart-recovery, so the store must be PG before `start()` runs. Store is set-once-before-start (never hot-swaps). `LocalEngine` owns the scheduler and builds it internally at 3 sites (local.rs:155/221/280), all hardcoding `SchedulerService::new()` (in-memory).

**Decision**: Approach A — constructor param. Add a `LocalEngine` constructor variant accepting `Option<Arc<dyn ScheduleStore>>` (default `None` → in-memory, preserving all existing paths). `main.rs` builds `PostgresScheduleStore` from `UC_DATABASE_URL` (mirroring `create_task_backend`), runs `scheduler::migration::run_migrations`, and passes the store into the engine constructor BEFORE `LocalEngine::new()`. Engine constructs `SchedulerService::with_store_and_dispatcher(pg_store, LoggingDispatcher)`. `start_scheduler` then swaps dispatcher/lock as today; store already PG. Recovery `list_tasks` in `start()` reads PG.

**Consequences**: Store set at construction → no RwLock needed, no lock on every store read. Adds one constructor variant (engine API surface +1). `ScheduleStore` + `PostgresScheduleStore` already re-exported (lib.rs:68,73); `with_store` already exists on the service (service.rs:154) — no new trait/API on SchedulerService. The fallback/error-recovery paths in main.rs (lines 414-419) keep in-memory scheduler (acceptable: those paths already signal degraded mode). Matches the `create_task_backend` precedent (#576) and the `with_store` design intent.

## Technical Notes

* Files to touch:
  * `crates/uc-engine/src/local.rs` — add constructor that accepts `Option<Arc<dyn ScheduleStore>>`; default paths unchanged.
  * `crates/uc-grpc-server/src/main.rs` — `create_schedule_store()` (mirrors `create_task_backend`), run migrations, pass into engine before `start_scheduler`.
  * `crates/uc-engine/src/scheduler/migration.rs` — already implemented; just needs calling.
  * `crates/uc-engine/src/lib.rs` — re-export `PostgresScheduleStore` + `ScheduleStore` if not already (check).
* No new crate deps. sqlx already used by PostgresTaskBackend / PostgresMetadataStore.
* Env: `UC_SCHEDULE_BACKEND` (new), `UC_DATABASE_URL` (existing, shared).
* Precedent PRs: #576 (task_backend write path), #577 (upsert race), #578 (startup load), #579 (NatsEventStore). This is the 4th activation in the series.

## Out of Scope

* Multi-gateway live-read consistency for schedules (same out-of-scope as task_backend — one-shot startup load, write-path keeps PG in sync).
* Hot-swapping the store at runtime (YAGNI — set-once-at-startup).
* A shared PG connection pool across metadata_store / task_backend / scheduler (isolation matches existing design; revisit only if pool count becomes a problem).
* Migration framework beyond `CREATE TABLE IF NOT EXISTS` (matches existing metadata/task_backend migration style).

## Implementation Plan (small PRs)

Mirrors the task_backend activation series (#576 write-path, #577 fix, #578 startup-load) — compressed since the write-path AND recovery already exist; only activation + injection is missing.

* **PR1: engine constructor + main.rs activation.** Add `LocalEngine::new_with_scheduler_store(config, Option<Arc<dyn ScheduleStore>>)` (storage-feature path) and thread store into `SchedulerService::with_store_and_dispatcher`. In `main.rs`: add `create_schedule_store()` (reads `UC_SCHEDULE_BACKEND` + `UC_DATABASE_URL`, builds `PostgresScheduleStore`, runs `scheduler::migration::run_migrations`, returns `Option<Arc<dyn ScheduleStore>>`); call before `LocalEngine::new()`; pass into constructor. Fallback paths unchanged (store=None → in-memory). Recovery via existing `start()` now reads PG.
* **PR2: spec doc.** Document `UC_SCHEDULE_BACKEND` in scheduler-spec (or checkpoint-spec sibling). Env table + activation note. Mirrors #580 (NatsEventStore spec).

## Acceptance Criteria

* [x] With `UC_SCHEDULE_BACKEND=postgres` + `UC_DATABASE_URL`, a scheduled cron job survives a server restart (re-registered by `start()` recovery reading PG).
* [x] Without those env vars, scheduler behaves exactly as today (in-memory, no crash, warn on misconfig).
* [x] `scheduled_tasks` + `execution_history` tables created on first PG startup; idempotent on subsequent.
* [x] `cargo check` (default + storage/messaging features) + `cargo test -p uc-engine` green.
* [x] No new dependencies; reuses existing sqlx pool pattern.
* [x] Spec documents `UC_SCHEDULE_BACKEND`.

## Completion Log

* **Activation** (PR #581 / 14a82f5): engine constructor variants
  (`new_with_scheduler_store[_deferred_text_index_restore]`),
  `create_schedule_store()` wiring in main.rs before engine construction,
  full-stack compose durable default, scheduler-spec persistence section,
  verify-command persistence fix (#585).
* **Rollout hardening** (this session, 2026-08-24):
  * Extracted pure `resolve_schedule_store_choice()` decision fn +
    cfg-split `connect_postgres_schedule_store()` — env-gating matrix now
    covered by 5 unit tests in uc-grpc-server (22 → 27 tests).
  * Standalone gateway (`docker-compose.gateway.yml`) now passes through
    `UC_SCHEDULE_BACKEND` (and `UC_EVENT_BACKEND`, same gap class) with
    `memory` defaults.
  * `run-cluster.sh` / `run-omp.sh` docker branches wire
    `UC_SCHEDULE_BACKEND=${UC_SCHEDULE_BACKEND:-postgres}` mirroring
    `UC_TASK_BACKEND`.
  * `docker/.env.example` documents the three durable-backend vars +
    shared `UC_DATABASE_URL`.
  * Crate doc header in main.rs documents the schedule backend.
  * Verified: workspace `cargo check --all-targets` ✓, `cargo test -p
    uc-engine` ✓, `cargo test -p uc-grpc-server` 27/27 ✓, workspace
    clippy ✓ (pre-existing uc-engine warning only), compose config ✓,
    script syntax (LF-normalized bash -n) ✓.
  * Note: live-PG restart-recovery remains covered by the ignored
    `storage_integration` suite (repo convention — requires a real PG),
    plus the service-level recovery tests in `scheduler/service.rs`.
