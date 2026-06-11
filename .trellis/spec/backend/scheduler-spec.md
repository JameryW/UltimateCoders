# Scheduler Code-Spec

> Task scheduling with night-time orchestration — executable contracts for implementation.

---

## Scenario: Night-Window Task Scheduling

### 1. Scope / Trigger

- Trigger: Any time a `ScheduledTask` is created (cron or one-shot), the scheduler must evaluate the night window before dispatching.
- Cross-layer: Rust `SchedulerService` → PyO3 → Python `Scheduler` → `Orchestrator.submit_task()`

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
| last_execution | Option\<DateTime\<Utc\>\> | Updated by system | No |
| next_execution | Option\<DateTime\<Utc\>\> | Computed from cron/execute_after | No |
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
