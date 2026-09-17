# Scheduler Code-Spec

> Task scheduling with night-time orchestration — executable contracts for implementation.

---

## Scenario: Night-Window Task Scheduling

### 1. Scope / Trigger

- Trigger: Any time a `ScheduledTask` is created (cron or one-shot), the scheduler must evaluate the night window before dispatching.
- Cross-layer: Rust `SchedulerService` (cron-fire) → `NatsSubmitDispatcher` publishes to `uc.task.submit` NATS → Python `_handle_submit` → `Orchestrator.submit_task()` (the Python `Scheduler` class was removed in #548; Rust owns scheduling, dispatch via NATS per the #553 ADR).

### 2. Signatures

#### Rust Core (`crates/uc-engine/src/scheduler/`)

```rust
// service.rs
pub struct SchedulerService { ... }

impl SchedulerService {
    pub fn new() -> Self;
    pub fn with_dispatcher(dispatcher: Arc<dyn ScheduleDispatcher>) -> Self;
    pub fn with_store_and_dispatcher(store: Arc<dyn ScheduleStore>, dispatcher: Arc<dyn ScheduleDispatcher>) -> Self;
    pub async fn add_cron_job(&self, task: ScheduledTask) -> Result<Uuid, EngineError>;
    pub async fn add_one_shot_job(&self, task: ScheduledTask) -> Result<Uuid, EngineError>;
    pub async fn remove_job(&self, id: &Uuid) -> Result<(), EngineError>;
    /// Pause (`false`) or resume (`true`) a job without deleting it; returns the
    /// updated task. See "Pause / Resume Semantics" below.
    pub async fn set_job_enabled(&self, id: &Uuid, enabled: bool) -> Result<ScheduledTask, EngineError>;
    /// Dispatch under the night-window guard, with the origin of the attempt.
    /// `dispatch_with_guard(id)` == `DispatchSource::Scheduled`. Only a
    /// `Scheduled` fire may re-arm the job (backoff retry after a transport
    /// failure, next window opening after a deferral); `Manual` records the
    /// outcome and leaves the plan and the retry budget untouched.
    pub async fn dispatch_with_guard_from(&self, id: &Uuid, source: DispatchSource) -> Result<(), EngineError>;
    pub fn list_jobs(&self) -> Vec<ScheduledTask>;
    pub fn get_job(&self, id: &Uuid) -> Option<ScheduledTask>;
    pub async fn start(&self) -> Result<(), EngineError>;
    pub async fn stop(&self) -> Result<(), EngineError>;
    pub fn is_running(&self) -> bool;
    pub fn set_night_window(&self, start: NaiveTime, end: NaiveTime, tz: &str) -> Result<(), EngineError>;
    pub fn clear_night_window(&self);
    pub fn check_night_window(&self, now: DateTime<Utc>) -> NightWindowCheck;
    pub fn get_execution_history(&self, task_id: &Uuid, limit: usize) -> Vec<ExecutionHistory>;
    pub async fn get_execution_history_from_store(&self, task_id: &Uuid, limit: i64) -> Result<Vec<ExecutionHistory>, EngineError>;
}

// night_window.rs
pub struct NightWindow { start: NaiveTime, end: NaiveTime, tz: Tz }
impl NightWindow {
    pub fn is_within_window(&self, now: DateTime<Tz>) -> bool;
    pub fn next_window_start(&self, now: DateTime<Tz>) -> DateTime<Tz>;
    pub fn next_window_end(&self, now: DateTime<Tz>) -> DateTime<Tz>;
    pub fn from_config(config: &NightWindowConfig) -> Result<Self, NightWindowError>;
}

// dispatcher.rs
pub trait ScheduleDispatcher: Send + Sync {
    fn dispatch(&self, task: &ScheduledTask) -> Result<(), EngineError>;
}
pub struct OrchestratorDispatcher { ... }  // feature-gated: messaging
pub struct LoggingDispatcher;              // always available, no-op
pub struct EngineSubmitDispatcher { ... }  // PR2: cron-fire → engine.submit_task (fire-and-forget)

// ── Design Decision: late-binding dispatcher ────────────────────────────
// EngineSubmitDispatcher needs Arc<LocalEngine>, but LocalEngine owns the
// SchedulerService (chicken-and-egg). Solution: SchedulerService starts with
// a LoggingDispatcher placeholder; the dispatcher field is
// Arc<RwLock<Arc<dyn ScheduleDispatcher>>> with a set_dispatcher() swap.
// After LocalEngine construction, call engine.init_scheduler_dispatcher()
// which builds EngineSubmitDispatcher(engine.clone()) + set_dispatcher().
//
// dispatch_with_guard reads the dispatcher from the RwLock per-fire
// (negligible: only on cron-fire, not per-request).
//
// ── Design Decision: fire-and-forget async-in-sync ──────────────────────
// dispatch() is sync (trait), submit_task is async. Solved with tokio::spawn:
// dispatch returns Ok(()) immediately (spawn succeeded), submit_task runs in
// background. dispatch_with_guard records Completed on spawn-success; if
// submit_task errs, the spawned task appends a Failed ExecutionHistory via
// engine.scheduler_service().record_execution() (record_execution is pub).
// block_in_place+block_on was REJECTED — blocking the runtime worker during
// the full task decomposition is unacceptable for a cron callback.

// store.rs
#[async_trait]
pub trait ScheduleStore: Send + Sync {
    async fn save_task(&self, task: &ScheduledTask) -> Result<(), EngineError>;
    async fn load_task(&self, id: &Uuid) -> Result<Option<ScheduledTask>, EngineError>;
    async fn list_tasks(&self, enabled_only: bool) -> Result<Vec<ScheduledTask>, EngineError>;
    async fn update_task(&self, task: &ScheduledTask) -> Result<(), EngineError>;
    async fn delete_task(&self, id: &Uuid) -> Result<(), EngineError>;
    async fn save_execution(&self, history: &ExecutionHistory) -> Result<(), EngineError>;
    async fn list_executions(&self, task_id: &Uuid, limit: i64) -> Result<Vec<ExecutionHistory>, EngineError>;
}
```

#### Python API (`crates/uc-python/src/scheduler.rs`)

> ⚠️ The pure-Python scheduler module was removed in `15b5ae3` (#548) once the Rust
> `SchedulerService` took over. The `Scheduler` class below is now the PyO3 export
> `PySchedulerService` from the `uc-python` extension; the implementation lives in
> `crates/uc-engine/src/scheduler/`.

```python
class Scheduler:
    def create_cron_job(self, description: str, cron_expression: str, *,
                        project_id: str | None = None,
                        night_window_start: str | None = None,
                        night_window_end: str | None = None,
                        timezone: str = "UTC") -> object
    def create_one_shot_job(self, description: str, execute_after: datetime | str, *,
                            project_id: str | None = None,
                            night_window_start: str | None = None,
                            night_window_end: str | None = None,
                            timezone: str = "UTC") -> object
    def cancel_job(self, task_id: str) -> bool
    def list_jobs(self) -> list
    def get_job(self, task_id: str) -> object | None
    def get_execution_history(self, task_id: str, limit: int = 50) -> list
    def set_night_window(self, start_time: str, end_time: str, timezone: str = "UTC") -> None
    def clear_night_window(self) -> None
    def start(self) -> None
    def stop(self) -> None
    def is_running(self) -> bool
    def load_config(self, path: str) -> None
```

### 3. Contracts

#### ScheduledTask Fields

| Field | Type | Constraints | Required |
|-------|------|------------|----------|
| id | UUID | Auto-generated | Yes |
| description | String | Non-empty | Yes |
| project_id | Option\<String\> | — | No |
| cron_expression | Option\<String\> | Valid croner 5-field syntax; mutually exclusive with execute_after | Conditional |
| execute_after | Option\<DateTime\<Utc\>\> | Must be in the future; mutually exclusive with cron_expression | Conditional |
| night_window_start | Option\<NaiveTime\> | HH:MM format | No |
| night_window_end | Option\<NaiveTime\> | HH:MM format; can be before start (cross-midnight) | No |
| timezone | String | Valid IANA timezone name (chrono-tz) | Yes (default "UTC") |
| enabled | bool | — | Yes (default true) |
| last_execution | Option\<DateTime\<Utc\>\> | Updated at the scheduler dispatch boundary (including a failed dispatch attempt) | No |
| next_execution | Option\<DateTime\<Utc\>\> | Cron: the next occurrence, computed on registration and after each dispatch. One-shot: **the attempt the scheduler owes** — normally `execute_after`, but a night-window deferral moves it to the next window opening (see "Deferred One-Shot Retry") and a failed dispatch moves it to the backoff retry (see "Dispatch Retry Budget"). `None` once dispatched, expired with nothing owed, or paused | No |
| dispatch_attempts | u32 | **Consecutive transport-level dispatch failures** (NATS unavailable / no worker accepted the job), i.e. `0` or a full budget on a never-failed job. Reset by the next successful dispatch. Deliberately *not* a task that was accepted and then failed on its own — the scheduler never retries those. Column `INTEGER NOT NULL DEFAULT 0`, not exposed over gRPC: the retry instant shows up as `next_run` and the attempt count is written into the history `result_summary` | No |
| created_at | DateTime\<Utc\> | Auto-set | Yes |
| updated_at | DateTime\<Utc\> | Auto-updated | Yes |

#### ExecutionHistory Fields

| Field | Type | Constraints | Required |
|-------|------|------------|----------|
| id | UUID | Auto-generated | Yes |
| scheduled_task_id | UUID | FK → scheduled_tasks.id (ON DELETE CASCADE) | Yes |
| started_at | DateTime\<Utc\> | — | Yes |
| completed_at | Option\<DateTime\<Utc\>\> | — | No |
| status | ExecutionStatus | One of: Completed, Failed, Skipped, Deferred | Yes |
| result_summary | Option\<String\> | — | No |
| deferred_reason | Option\<String\> | Required when status=Deferred | Conditional |

#### Night Window Behavior

- Window defined by `(start_time, end_time, timezone)`
- Cross-midnight: if `end < start`, window spans midnight (e.g., 22:00→06:00)
- `is_within_window(now)`: For cross-midnight: `time >= start || time < end`; For same-day: `time >= start && time < end`
- Guard check happens before dispatch: outside window → record `Deferred` history, skip dispatch
- A deferred **one-shot** is re-armed for the moment the window reopens (see "One-Shot Re-Arm"); a deferred cron job needs nothing
- The guard exposes the reopening instant as data (`next_window_opening() -> Option<DateTime<Utc>>`); `check_night_window()` is expressed through it and builds the shared `outside_window_error(...)`, so the public message and the scheduling decision cannot drift
- Window open/close events published to NATS `schedule.window.opened` / `schedule.window.closed` (feature-gated: messaging)

#### One-Shot Re-Arm (`rearm_one_shot_at(task_id, when)`)

Shared by two callers: the night-window deferral below, and the dispatch retry
budget. It reads the **current** registry snapshot rather than a caller's clone (a
whole-task write from a stale copy would erase the attempt count
`mark_execution_started` has just persisted), and never moves an already-owed
attempt **earlier**, so one event cannot pull another's pending retry forward.

The runtime one-shot is **consumed by firing**. If the guard then refuses the
dispatch, the job has run out of lives: without an explicit re-arm the promised
run is gone, while the `Deferred` record the guard just wrote reads "next window
starts at X" — a promise nothing intended to keep.

On deferral the service commits the window opening as the owed attempt, in this
order:

1. `job_metadata` snapshot gets `next_execution = Some(opening)` → the dashboard
   `next_run` now shows *when the retry will actually happen*.
2. `store.update_task` persists it (failure logged, not fatal). Committing before
   scheduling is the point: a restart between the two steps re-registers the
   retry from the durable record, because `one_shot_attempt_at` prefers a future
   `next_execution` over the passed `execute_after`.
3. Registration with the runtime scheduler happens **detached and type-erased**
   (`tokio::spawn(Self::rearm_registration_task(..) -> BoxFuture<'static, ()>)`).
   The erased signature is load-bearing: the chain
   `register_one_shot_with_scheduler → fired-callback → dispatch_with_guard →
   rearm_deferred_one_shot → register_one_shot_with_scheduler` is a type cycle,
   and without a declared boundary the callback's future is neither inferrable
   nor `Send`.
4. Because of (3), the fired one-shot callback forgets its runtime UUID **first**
   (not on exit): a trailing forget could otherwise erase the fresh mapping the
   re-arm just inserted, leaving a live runtime job that `remove_job` no longer
   knows about.

Consequences that are intentional: a deferral while the service is stopped still
persists the retry time and registers on the next `start()`; if the window has
moved by the time the retry comes due, the job defers again and re-arms again
(each re-arm waits for a real future opening, so this cannot spin); and pausing a
job drops a pending retry, since `pause` clears `next_execution` and a resumed
expired one-shot owes nothing.

When two re-arms race for the same task (an operator trigger during the due
callback), the later one **replaces** the runtime-id mapping and unregisters the
job it superseded — an overwritten mapping would leave a live runtime job that
`remove_job` can no longer find. It follows the file's existing lock order
(job scheduler read guard held while the id map is written), so it cannot
deadlock against `remove_job` / `unregister_runtime_job`.

#### Dispatch Retry Budget (no worker)

`ScheduleDispatcher::dispatch` returning `Err` means the job never reached a
worker (NATS down, or no registered worker took it). The task itself is valid, so
it is recorded as `Skipped` rather than `Failed` — but for a one-shot that
consumed attempt was also its only one, so the scheduler owes a bounded retry.

| Situation | Spends the budget? | Re-arms? |
|-----------|--------------------|----------|
| `Scheduled` fire of a **one-shot**, transport `Err` | yes (`dispatch_attempts += 1`) | yes, at the backoff instant while `attempts <= MAX_DISPATCH_RETRIES` (3) |
| Same, budget exhausted | counted, then nothing further | no — `next_execution = None` |
| **Cron** job, transport `Err` | no | no — its next tick already *is* the retry |
| **`Manual`** trigger, transport `Err` | no | no — `next_execution` stays exactly as planned |
| Accepted, then failed on its own (`Failed`) | no | no — retrying a task that is itself broken just burns the same credits again |

- **Backoff:** `retry_delay_for(attempt)` = 60s doubled per attempt, capped at
  900s → `60s, 120s, 240s`, then exhausted. One initial dispatch plus three
  retries: a fifth failure is a different problem than a transient one.
- **History wording carries the state** instead of a new proto field:
  `"Dispatch skipped (no worker): <err>; retry 1 of 3 at <instant>"`, finally
  `"…; retries exhausted after 4 attempts"`. `uc_scheduler` renders a Skipped
  summary in its error slot, and the dashboard shows the retry instant as
  `next_run`.
- **Budget maintenance lives in `mark_execution_started(task_id, started_at,
  outcome)`**, under the same monotonicity guard as `last_execution` so an
  out-of-order completion cannot double-count: `Succeeded` → 0,
  `FailedRetryable` → +1, `FailedObserved` → unchanged.
- **Durable on purpose.** Because `dispatch_attempts` is a column, a restart
  cannot launder a spent budget into a fresh one; recovery also keeps the owed
  retry instant, and a pending retry is *not* recorded as a missed run.
- A retry that is itself deferred re-arms to `max(window opening, retry instant)`
  — the two events cannot pull each other earlier.

#### Missed One-Shot Recovery (`start()`)

A one-shot whose `execute_after` passed while the gateway was down cannot be
scheduled (`one_shot_duration` refuses a past instant). Two wrong answers to
avoid: **back-firing it at boot** (an operator chose that instant for a reason —
a maintenance window, a quiet hour — and process start-up is not a licence to
spring a scheduled run), or **dropping it silently** (the historical behaviour:
one `warn!` line and nothing in the UI, leaving a lost run indistinguishable from
a job that simply has not come due).

Recovery therefore appends **exactly one** `ExecutionHistory` record:

| Field | Value | Why |
|-------|-------|-----|
| `status` | `Skipped` | no dispatch was made, and none will be |
| `started_at` | the missed `execute_after` | the timeline shows *when the promise broke*, not when this process noticed |
| `completed_at` | `None` | pairing the missed instant with "now" renders as a fake run *duration* rather than as downtime |
| `result_summary` | `"Missed: execute_after <ts> passed while the gateway scheduler was not running"` | surfaced verbatim by `uc_scheduler` status (a Skipped summary maps onto `error`) and the dashboard history list |

Suppression — the run is not "missed" if anything proves it was handled, and a
restart must not repeat the record:

- Nothing owed yet: `one_shot_attempt_at(task, now)` is `Some(..)` — the original
  `execute_after` is still future, **or** a previous deferral re-armed a retry.
- `last_execution >= execute_after` → a real dispatch already happened.
- newest durable history row with `started_at >= execute_after` **and status
  `Completed` / `Failed` / `Skipped`** → an attempt was made, or an earlier
  restart already recorded this exact miss (idempotence across N restarts).
- A **`Deferred`** row does *not* suppress. The guard refusing to dispatch is
  precisely the case where the promised run is still lost — a one-shot is never
  retried after a deferral — so letting `Deferred` mask the miss would leave
  "next window starts at X" as the last thing anyone ever sees about a run that
  will not happen.
- `store.list_executions` failing → record nothing, `warn!`. Without a dedup
  signal the write would duplicate on every boot.

Exclusions: **cron jobs never produce a miss** — a cron expression is a standing
schedule, not a single promised run, so recomputing `next_execution` is the
entire recovery obligation. Paused one-shots are excluded too: a job stopped on
purpose did not have its run *missed*.

The record uses an **ordered insert**, not `record_execution`'s append —
`recover_execution_history` keeps the dashboard cache ascending by
`started_at`, and a recovered miss carries a past timestamp.

#### Pause / Resume Semantics (`set_job_enabled`)

Removal is destructive: `delete_task` cascades the job's `execution_history`
away. Pausing is the reversible control for "stop firing this for a while" —
it keeps the durable record *and* its history.

| Operation | Runtime scheduler | `enabled` | `next_execution` | `last_execution` / history |
|-----------|-------------------|-----------|------------------|-----------------------------|
| Pause (`enabled = false`) | unregistered | `false` | cleared (`None`) | untouched |
| Resume (`enabled = true`) | registered when the service is started | `true` | recomputed from *now* | untouched |
| Toggle to the current state | untouched | unchanged | unchanged | untouched (no-op, no durable write) |
| Manual trigger of a paused job | not registered | unchanged | stays `None` | updated as usual |

- **Ordering**: the runtime (un)registration happens *before* the visible
  snapshot is committed, so a resume whose registration fails leaves the job
  exactly as it was instead of reporting an enabled job that never fires.
- **A paused job never advertises a run.** `mark_execution_started` re-applies
  the rule after a manual trigger, so `next_execution` cannot reappear while the
  job is paused (a manual trigger is still honoured — it is an explicit operator
  action).
- **Expired one-shot**: resuming a one-shot whose `execute_after` already passed
  sets `enabled = true` with `next_execution = None` and registers nothing — the
  same shape `add_one_shot_job` produces for a past timestamp.
- **Persistence is best-effort**: `update_task` failure is logged, not fatal,
  matching the dispatch-metadata contract (in-memory + runtime already agree).
- **Cross-layer surface**: `EngineApi::set_scheduler_job_enabled(job_id, enabled)`
  → `SchedulerJobEnabledResult { success, job_id, enabled, error }` (the trait's
  default impl reports `success: false` + "Scheduler not available"); gRPC
  `DashboardService::SetSchedulerJobEnabled`; the `uc_scheduler` LLM tool's
  `pause` / `resume` actions; `/uc schedule pause|resume <job-id>`; and the
  Dashboard SchedulerPanel Pause/Resume button (`onSetJobEnabled`).

#### `uc.scheduler.yaml` Config Contract (PR1/PR2)

Gateway loads `uc.scheduler.yaml` at boot (`UC_SCHEDULER_CONFIG` env → `./uc.scheduler.yaml`). Missing file = idle scheduler (no behavior change, opt-in). `SchedulerFileConfig` (Rust, `crates/uc-engine/src/scheduler/config.rs`):

```yaml
night_window:        # optional top-level; absent = no window (jobs fire any time)
  start: "22:00"     # HH:MM
  end: "06:00"
  timezone: "Asia/Shanghai"  # IANA; defaults "UTC"
jobs:
  - description: nightly build        # required
    project_id: ""                    # optional
    cron: "0 22 * * *"               # cron OR execute_after, mutually exclusive
    # execute_after: "2026-09-01T09:00:00Z"  # RFC-3339, one-shot
    night_window: { start: "12:00", end: "13:00" }  # optional per-job override
    enabled: true                     # default true
```

- `resolve()` validates: a job with neither/both `cron`+`execute_after` → error; bad HH:MM → error; bad RFC-3339 → error.
- `default_night_window` + per-job `night_window` are `Option<NightWindowConfig>` — when no top-level window is declared, `None` (NOT 22:00-06:00 UTC). `main.rs` only calls `set_night_window` when `Some`.
- A per-job `night_window` overrides the top-level default for that job only.

#### Schedule Persistence (`UC_SCHEDULE_BACKEND`)

The scheduler persists jobs + execution history via the `ScheduleStore` trait
(`InMemoryScheduleStore` for tests, `PostgresScheduleStore` for production,
the latter behind the `storage` feature). Activation is env-gated in the
gateway binary (`uc-grpc-server/src/main.rs::create_schedule_store`), mirroring
`UC_TASK_BACKEND` / `UC_EVENT_BACKEND`:

| Env | Default | Description |
|-----|---------|-------------|
| `UC_SCHEDULE_BACKEND` | _(unset = in-memory)_ | `postgres` → `PostgresScheduleStore`; unset/`memory` → in-memory (jobs lost on restart) |
| `UC_DATABASE_URL` | _(empty)_ | PostgreSQL URL. Required when `UC_SCHEDULE_BACKEND=postgres`; empty/missing → warn + in-memory fallback |

- **Construction**: `PostgresScheduleStore::connect(url)` builds a dedicated
  pool (`max_connections(5)`) and runs idempotent migrations
  (`scheduled_tasks` + `execution_history` tables + indexes, `scheduler/migration.rs`).
  Migrations stay additive-only: new columns land in `CREATE TABLE` for fresh
  installs *and* an `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for rows written
  before they existed (`verify_command`, `dispatch_attempts`), so a rolling
  deploy needs no manual DDL.
  Injected into `LocalEngine::new_with_scheduler_store(config, Some(store))`
  BEFORE `SchedulerService::start()` runs. The store is set-once-at-construction
  (not hot-swappable) — no RwLock.
- **Write path** (already wired in `service.rs`): `add_cron_job`/`add_one_shot_job`
  → `save_task`; `remove_job` → `delete_task`; `set_job_enabled` (pause/resume)
  → `update_task`; dispatch attempts update
  `last_execution`/`next_execution` through `update_task`; `record_execution`
  → `save_execution`.
- **Restart recovery** (already wired in `service.rs::start()`): `list_tasks(false)`
  loads **every** persisted task — enabled *and* paused — into `job_metadata`, so a
  paused job stays visible and resumable after a restart (a job that vanished from
  the registry could never be resumed). Each task's `next_execution` is recomputed
  from the current UTC clock when it is enabled, and forced to `None` when it is
  paused; the refreshed snapshot is persisted when it changed. Only enabled tasks
  are registered with the `JobScheduler`. With the PG backend, jobs survive a
  gateway restart without stale next-run metadata.
  `list_jobs()` returns the active snapshot in stable `created_at` order (with
  UUID as a tie-breaker), so Dashboard refreshes do not reorder jobs after
  HashMap recovery.
  Startup also best-effort hydrates the dashboard cache with the latest 50
  execution records across persisted tasks (including disabled jobs), merges
  the per-task newest-first queries into chronological order, and keeps the
  durable store as the source of truth for older history. Live recording keeps
  the existing in-memory behavior for the default memory backend, and a failed
  history slice is logged without blocking healthy job recovery.
- **Fallback**: missing `UC_DATABASE_URL` / connection failure / `storage`
  feature disabled → warn + in-memory (no crash). Default (unset) = zero
  behavior change for existing deploys.
- **Timestamp resolution is microseconds.** `TIMESTAMPTZ` stores µs while
  `Utc::now()` carries ns, so a value that round-trips the database differs below
  the microsecond. Never compare a persisted `next_execution` / `execute_after`
  against a freshly computed one at full precision — normalize first, or compare
  at µs. A nanosecond-equality assert on a round-tripped instant fails against a
  live server and passes against the in-memory store, so it must not be used.
- **Live coverage**: `crates/uc-engine/tests/storage_integration.rs` →
  `schedule_postgres_tests` exercises the legacy `ALTER` upgrade path inside a
  throwaway schema (never touching `public.scheduled_tasks`, and cleaned up even
  when an assertion fails) and round-trips retry state through the real store:
  `docker compose -f docker/docker-compose.yml up -d postgres` then
  `cargo test -p uc-engine -- --ignored schedule_postgres`.
- **Deployment surfaces**: full-stack `docker/docker-compose.yml` defaults
  `UC_SCHEDULE_BACKEND=postgres` (durable); the standalone gateway
  (`docker-compose.gateway.yml` via `run-gateway.sh`) passes the var through
  with `memory` default (opt-in via env / docker `.env`), as do the docker
  branches of `run-cluster.sh` / `run-omp.sh` (postgres default, mirroring
  `UC_TASK_BACKEND`). The gating decisions live in pure `resolve_*_choice`
  helpers sharing the `BackendChoice` enum (`Durable(url)` / `Memory {
  reason, warn }`) so all three matrices — tasks, events, schedules — are
  unit-tested in uc-grpc-server.
- **Out of scope**: multi-gateway live-read consistency (one-shot startup load;
  write-path keeps PG in sync going forward — same model as `UC_TASK_BACKEND`).

### 4. Validation & Error Matrix

| Condition | Error | Code |
|-----------|-------|------|
| Both cron_expression and execute_after set | `InvalidInput` | "Must specify either cron_expression or execute_after, not both" |
| Neither cron_expression nor execute_after set | `InvalidInput` | "Must specify either cron_expression or execute_after" |
| Invalid cron expression syntax | `InvalidInput` | "Invalid cron expression: {detail}" |
| Invalid IANA timezone name | `InvalidInput` | "Invalid timezone: {name}" |
| execute_after is in the past | `InvalidInput` | "execute_after must be in the future" |
| Task ID not found (remove/get) | `NotFound` | "Scheduled task not found: {id}" |
| Task ID not in the registry (pause/resume) | `TaskError` | "Scheduled task not found: {id}" |
| Pause/resume to the state the job is already in | (no error) | no-op — current task returned, no durable write |
| Scheduler already running (start) | `InvalidState` | "Scheduler is already running" |
| Scheduler not running (stop) | `InvalidState` | "Scheduler is not running" |
| `uc.scheduler.yaml` missing | (no error) | idle scheduler — opt-in, no behavior change |
| `uc.scheduler.yaml` bad YAML / bad config | logged + skipped | gateway starts, scheduler idle |
| cron-fire submit_task returns Err | `Failed` ExecutionHistory | appended by spawned task (fire-and-forget can't surface to dispatch return) |
| scheduled one-shot dispatch returns Err | `Skipped` + re-arm at backoff instant | summary names `retry N of 3 at <instant>`; budget is durable |
| same, with `dispatch_attempts` already at the bound | `Skipped`, `next_execution = None` | "retries exhausted after N attempts"; no further re-arm |
| cron dispatch returns Err | `Skipped`, budget untouched | next cron tick is the retry |
| manual trigger returns Err | `Skipped`, budget and plan untouched | `DispatchSource::Manual` may not reschedule |
| one-shot expired during gateway downtime | `Skipped` ExecutionHistory | recorded at recovery with `started_at` = the missed instant; never back-fired |
| `Deferred` row exists for that one-shot | `Skipped` miss still recorded | a deferral was never retried, so the run is still lost |
| `list_executions` fails during recovery | (no record) | logged — without a dedup signal the write would repeat on every boot |
| Duplicate task ID (save) | `AlreadyExists` | "Scheduled task already exists: {id}" |

### 5. Good/Base/Bad Cases

**Good**: Cron job with night window
```python
scheduler.create_cron_job(
    description="Rebuild index",
    cron_expression="0 22 * * *",
    night_window_start="22:00",
    night_window_end="06:00",
    timezone="Asia/Shanghai"
)
```

**Base**: One-shot deferred task without night window
```python
scheduler.create_one_shot_job(
    description="Run code review",
    execute_after="2026-06-12T23:00:00+08:00"
)
```

**Bad**: Both cron and execute_after specified
```python
scheduler.create_cron_job(
    description="Invalid",
    cron_expression="0 22 * * *",
    execute_after="2026-06-12T23:00:00Z"  # ERROR: mutually exclusive
)
```

### 6. Tests Required

| Test | Type | Assertion |
|------|------|-----------|
| NightWindow cross-midnight (22:00-06:00, check 23:00) | Unit | `is_within_window` returns true |
| NightWindow cross-midnight (22:00-06:00, check 12:00) | Unit | `is_within_window` returns false |
| NightWindow same-day (09:00-17:00, check 12:00) | Unit | `is_within_window` returns true |
| NightWindow same-day (09:00-17:00, check 22:00) | Unit | `is_within_window` returns false |
| NightWindow timezone (Shanghai UTC+8) | Unit | Correct conversion from UTC |
| ScheduledTask cron creation | Unit | `is_cron()` true, `is_one_shot()` false |
| ScheduledTask one-shot creation | Unit | `is_one_shot()` true, `is_cron()` false |
| ScheduleStore CRUD | Unit | Save → load → update → delete round-trip |
| ScheduleStore cascade delete | Unit | Deleting task removes associated executions |
| SchedulerService night window guard | Unit | Outside window → Deferred execution history |
| SchedulerService persistence | Unit | Add job → save to store → restart → job recovered |
| Pause unregisters + clears `next_execution` | Unit | `scheduler_job_ids` loses the task, store row has `enabled=false`, `next_execution=None` |
| Resume re-registers + recomputes | Unit | `next_execution > now`, runtime UUID present again |
| Pause keeps `last_execution` + history | Unit | History row survives the toggle (removal would cascade it) |
| Toggle to current state is a no-op | Unit | `updated_at` unchanged on the second call |
| Expired one-shot resume | Unit | `enabled=true`, `next_execution=None`, nothing registered |
| Manual trigger of a paused job | Unit | `last_execution` set, `next_execution` stays `None` |
| Pause survives a restart | Unit | Fresh service on the same store lists the paused job, does not register it, and resumes it |
| Recovery loads all, registers only enabled | Unit | `job_count == 2`, paused job present with `next_execution=None` and no runtime registration |
| Expired one-shot records one `Skipped` | Unit | row exists with `started_at == execute_after`, `status=Skipped`, `completed_at=None`, summary contains "Missed"; job stays listed with `next_execution=None` |
| Missed one-shot is not re-recorded | Unit | two `start()` calls on the same store → still exactly one history row |
| Already-dispatched one-shot | Unit | `last_execution >= execute_after` → no history written |
| Deferred one-shot still records the miss | Unit | a pre-existing `Deferred` row does not suppress; the second restart does not duplicate |
| Paused one-shot | Unit | `enabled=false` → no miss recorded |
| Cron never produces a miss + ordered cache | Unit | cron job has no history rows; two recovered misses land ascending by `started_at` |
| Deferred one-shot re-arms | Unit | deterministic always-closed window (`start == end`) → dispatch refused, `next_execution` becomes a future opening, exactly one `Deferred` record |
| Re-arm survives a restart and is not a miss | Unit | gateway dies before the retry → recovery keeps `next_execution`, records no `Skipped` |
| `one_shot_attempt_at` precedence | Unit | future `next_execution` beats a passed `execute_after`; falls back to `execute_after`; `None` when nothing is owed |
| Failed dispatch re-arms one-shot | Unit | `dispatch_attempts == 1`, future `next_execution`, summary contains "retry 1 of" |
| Retry budget is bounded | Unit | pre-seeded `dispatch_attempts = MAX` → no re-arm, `next_execution = None`, summary says "exhausted" |
| Success resets the budget | Unit | `dispatch_attempts` back to 0 in memory **and** the durable row |
| Manual trigger is inert | Unit | `DispatchSource::Manual` failure leaves `dispatch_attempts` and `next_execution` untouched, summary announces no retry |
| Cron failure spends no budget | Unit | `dispatch_attempts == 0`, next tick still advertised |
| Backoff schedule | Unit | 60/120/240s, capped at 900s (no overflow at attempt 64) |
| Budget + owed retry survive a restart | Unit | fresh service on the same store keeps `dispatch_attempts == 1` and the future retry |
| Recovery run-state discrimination (widened) | Unit | stale *past* `next_execution` on an expired one-shot is cleared in memory **and** the store, while a *future* (re-armed) one is preserved |
| Orchestrator night exclusive mode | Unit | `night_window_active=True` → non-scheduled tasks queued |
| Orchestrator flush pending | Unit | `flush_pending_tasks()` executes all queued tasks |
| Orchestrator scheduled task bypass | Unit | Scheduled tasks execute even during night window |
| YAML config loading | Integration | Parse → validate → create jobs |
| Cron validation (invalid) | Unit | Returns `EngineError::InvalidInput` |
| DST ambiguity handling | Unit | `earliest()`/`latest()` instead of `single()` |

### 7. Wrong vs Correct

#### Wrong: Using Local timezone for night window check

```rust
// BAD: couples to system timezone
let now = chrono::Local::now();
if window.is_within_window(now.with_timezone(&window.tz)) { ... }
```

#### Correct: Explicitly convert from UTC

```rust
// GOOD: deterministic, matches PRD "store UTC, evaluate in config timezone"
let now = chrono::Utc::now().with_timezone(&window.tz);
if window.is_within_window(now) { ... }
```

#### Wrong: DST handling with .single()

```rust
// BAD: returns None during DST transition, silently falls back
let today_start = now.date().and_time(self.start)
    .and_local_timezone(self.tz)
    .single()
    .unwrap_or(now);
```

#### Correct: DST handling with .earliest()/.latest()

```rust
// GOOD: for window start, pick earliest valid instance during DST ambiguity
let today_start = now.date().and_time(self.start)
    .and_local_timezone(self.tz)
    .earliest()
    .unwrap_or(now);
```

---

## Feature Gates

| Feature | Enables | Default |
|---------|---------|---------|
| `scheduler` | `tokio-cron-scheduler`, `croner`, `chrono-tz` in uc-engine | Off |
| `storage` | PostgreSQL ScheduleStore, migrations | On |
| `messaging` | OrchestratorDispatcher (NATS), window events | On |

---

## YAML Configuration Schema

```yaml
night_window:
  start: "22:00"       # Required if night_window section present
  end: "06:00"         # Required if night_window section present
  timezone: "UTC"      # Optional, default "UTC"

tasks:
  - description: "..."          # Required
    cron_expression: "..."      # Conditional: either this or execute_after
    execute_after: "..."        # Conditional: ISO 8601 datetime
    project_id: "..."           # Optional
    night_window_start: "..."   # Optional: overrides global
    night_window_end: "..."     # Optional: overrides global
    timezone: "..."             # Optional: overrides global
    enabled: true               # Optional, default true
```

Validation rules:
- Either `cron_expression` or `execute_after` must be set (not both, not neither)
- Time format: `HH:MM` (24-hour)
- `execute_after` format: ISO 8601 datetime string

---

## Orchestrator Night-Window Exclusive Mode

When the night window is active, the Orchestrator enters **exclusive mode**: scheduled tasks bypass the queue and execute immediately, while real-time tasks are deferred to `_pending_tasks` until the window closes.

### Python API (`python/ultimate_coders/agent/orchestrator.py`)

```python
class Orchestrator:
    # Properties
    night_window_active: bool          # Read-only property
    pending_task_count: int            # Number of deferred tasks

    # Methods
    def set_night_window_active(self, active: bool) -> None
    async def flush_pending_tasks(self) -> list[Task]

    # Scheduling delegation (requires scheduler= in __init__)
    def schedule_task(self, description: str, *,
                      cron: str | None = None,
                      execute_after: str | None = None,
                      project_id: str | None = None,
                      night_window_start: str | None = None,
                      night_window_end: str | None = None,
                      timezone: str = "UTC") -> ScheduledTask
```

### Contracts

| Condition | Behavior |
|-----------|----------|
| `night_window_active=True` + `_scheduled=False` | Task status → `PAUSED`, appended to `_pending_tasks` |
| `night_window_active=True` + `_scheduled=True` | Task executes normally (bypasses queue) |
| `night_window_active=False` | All tasks execute normally |
| `flush_pending_tasks()` called | All pending tasks re-submitted, `_pending_tasks` cleared |
| `schedule_task()` with no scheduler | `RuntimeError("No scheduler configured")` |
| `schedule_task()` with neither cron nor execute_after | `ValueError("Must specify either cron or execute_after")` |

### Event Flow

```
NATS schedule.window.opened → Orchestrator.set_night_window_active(True)
NATS schedule.window.closed → Orchestrator.set_night_window_active(False)
                              → Orchestrator.flush_pending_tasks()
```

> **Gotcha**: The `_scheduled` flag is an internal parameter on `submit_task()`. It should **never** be set by external callers — only by the scheduler dispatch path. Setting it incorrectly will bypass the night-window queue for real-time tasks.
