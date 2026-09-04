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

#### Python API (`python/ultimate_coders/agent/scheduler.py`)

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
| next_execution | Option\<DateTime\<Utc\>\> | Computed on registration and after each dispatch from cron/execute_after; one-shot becomes `None` after dispatch | No |
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
- Window open/close events published to NATS `schedule.window.opened` / `schedule.window.closed` (feature-gated: messaging)

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
  Injected into `LocalEngine::new_with_scheduler_store(config, Some(store))`
  BEFORE `SchedulerService::start()` runs. The store is set-once-at-construction
  (not hot-swappable) — no RwLock.
- **Write path** (already wired in `service.rs`): `add_cron_job`/`add_one_shot_job`
  → `save_task`; `remove_job` → `delete_task`; dispatch attempts update
  `last_execution`/`next_execution` through `update_task`; `record_execution`
  → `save_execution`.
- **Restart recovery** (already wired in `service.rs::start()`): `list_tasks(true)`
  → re-register each persisted cron/one-shot job with the `JobScheduler` + load
  into `job_metadata`. With the PG backend, jobs survive a gateway restart.
- **Fallback**: missing `UC_DATABASE_URL` / connection failure / `storage`
  feature disabled → warn + in-memory (no crash). Default (unset) = zero
  behavior change for existing deploys.
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
| Scheduler already running (start) | `InvalidState` | "Scheduler is already running" |
| Scheduler not running (stop) | `InvalidState` | "Scheduler is not running" |
| `uc.scheduler.yaml` missing | (no error) | idle scheduler — opt-in, no behavior change |
| `uc.scheduler.yaml` bad YAML / bad config | logged + skipped | gateway starts, scheduler idle |
| cron-fire submit_task returns Err | `Failed` ExecutionHistory | appended by spawned task (fire-and-forget can't surface to dispatch return) |
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
