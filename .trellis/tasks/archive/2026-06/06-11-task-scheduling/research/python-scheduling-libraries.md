# Research: Python Scheduling Libraries

- **Query**: Compare APScheduler (v4), Celery Beat, and custom asyncio solutions for scheduling in UltimateCoders Python Agent layer
- **Scope**: Mixed (internal codebase + external libraries)
- **Date**: 2026-06-11

## Findings

### Existing Codebase Context

| File Path | Description |
|---|---|
| `python/ultimate_coders/agent/orchestrator.py` | Orchestrator class with `submit_task()`, `decompose_task()`, `assign_subtask()` |
| `python/ultimate_coders/agent/worker.py` | Worker class for executing subtasks |
| `python/ultimate_coders/agent/types.py` | Task/Subtask/WorkerInfo dataclasses, TaskStatus enum |
| `python/ultimate_coders/agent/rate_limiter.py` | TokenBucket, RateLimiter, CircuitBreaker |
| `python/ultimate_coders/agent/__init__.py` | Module exports (Orchestrator, Worker, sandbox, etc.) |
| `pyproject.toml` | Maturin build, Python >=3.9, pytest with asyncio_mode="auto" |

Key observations:
- The Orchestrator is **not currently async** in its core methods. `submit_task()` and `handle_subtask_result()` are defined as `async` but do not use `await` internally for scheduling -- they await LLM calls but the task lifecycle is synchronous.
- No scheduling infrastructure exists in the Python layer at all.
- The project uses `pytest` with `asyncio_mode = "auto"`, indicating async support throughout the test suite.
- The Orchestrator has an in-memory `self.tasks: Dict[str, Task]` -- no persistence of task state.
- `TaskStatus.PAUSED` exists but is never set programmatically.
- No `asyncio` event loop management or background task infrastructure.

---

### Library Comparison

#### 1. APScheduler v4 (4.0.0a6 -- pre-release)

- **Repo**: https://github.com/agronholm/apscheduler (7,525 stars)
- **Latest stable**: v3.11.2 (in-process scheduler, cron-like capabilities)
- **Latest v4**: 4.0.0a6 (alpha/pre-release)

**v4 Features (from README and docs)**:
- **Async support**: Both synchronous and asynchronous flavors. Native asyncio and Trio support.
- **Scheduling mechanisms**:
  - Cron-style scheduling
  - Interval-based scheduling (even intervals)
  - Calendar-based scheduling (intervals of X years/months/weeks/days, same time of day)
  - One-off scheduling (specific date/time)
  - Combining triggers (union of multiple schedules)
  - Custom trigger classes
- **Persistence**: PostgreSQL, MySQL, SQLite, MongoDB backends for schedules and jobs.
- **Event brokers** (for multi-scheduler/worker coordination): PostgreSQL, Redis, MQTT.
- **Scalability**: Multiple schedulers and workers can use a shared data store for HA and horizontal scaling.
- **Late start handling**: Configurable max lateness for job start time.
- **Jitter**: Adjustable random delays to prevent thundering herd.
- **Concurrency limits**: Max simultaneous jobs per task/function.
- **Web integration**: WSGI and ASGI compatible.
- **CRITICAL WARNING**: The v4 README explicitly states: *"The v4.0 series is provided as a pre-release and may change in a backwards incompatible fashion without any migration pathway, so do NOT use this release in production!"*

**v3 Features (stable)**:
- Mature, production-tested
- Cron, interval, and date triggers
- In-memory, SQLAlchemy, MongoDB, Redis, ZooKeeper, etcd job stores
- ThreadPoolExecutor and ProcessPoolExecutor for sync jobs
- AsyncIOScheduler for async jobs (but less deeply integrated than v4)
- Timezone support via pytz/dateutil
- **Limitation**: v3 is primarily designed for in-process scheduling, not distributed scheduling. The async support is a bolt-on rather than native.

**Integration with UltimateCoders**:
- APScheduler v4's PostgreSQL persistence aligns with the project's PostgreSQL metadata store.
- v4's event brokers could coordinate multiple Orchestrator instances.
- v4's combining triggers could implement night-window scheduling (e.g., cron trigger AND time-window guard).
- **However**, v4 is explicitly not production-ready. v3 is stable but less async-native.
- The Orchestrator would need to register scheduled tasks that call `orchestrator.submit_task()`.

**Risk Assessment**:
- v4 alpha: Feature-rich but unstable. API may change without migration path.
- v3 stable: Battle-tested but limited async integration. Would need careful wrapping.

#### 2. Celery Beat

- **Repo**: https://github.com/celery/celery (28,571 stars)
- **Description**: Distributed Task Queue. Beat is the scheduling component.

**Celery Beat Features**:
- **Cron support**: Full crontab-style scheduling via `celery.schedules.crontab`.
- **Interval support**: `celery.schedules.schedule(run_every=...)`.
- **Persistence**: Schedule state stored in a local file (`celerybeat-schedule`) by default. Can use `django-celery-beat` for database-backed persistence.
- **Timezone**: Supports timezone-aware scheduling via Celery's timezone settings.
- **Distributed**: Designed for distributed task execution across multiple workers.
- **Integration**: Requires a message broker (Redis, RabbitMQ). The project uses NATS JetStream, not a Celery-compatible broker.

**Limitations for UltimateCoders**:
- **Broker incompatibility**: Celery requires Redis or RabbitMQ as a broker. The project uses NATS JetStream. Adding Redis/RabbitMQ just for Celery would be a significant infrastructure addition.
- **Heavyweight**: Celery is a full distributed task queue. The project already has its own Orchestrator-Worker pattern. Adding Celery would create two parallel worker systems.
- **Sync-first**: Celery workers are primarily synchronous. Async support exists via `gevent` or `eventlet` patches but is not native asyncio.
- **Beat is a single process**: `celery-beat` is a single scheduler process. If it crashes, scheduled tasks are missed until restart (unless using a database-backed schedule with `django-celery-beat`).
- **Python-only**: No Rust integration. Would not help with scheduling in the Rust engine.

**Verdict**: Celery Beat is ill-suited for UltimateCoders because:
1. It requires a broker (Redis/RabbitMQ) that the project does not use
2. It duplicates the existing Orchestrator-Worker pattern
3. It is sync-first, not asyncio-native
4. It has no Rust integration path

#### 3. Custom asyncio Solution

- **Approach**: Build scheduling directly on Python's `asyncio` primitives.

**Core Components**:
```python
import asyncio
from datetime import datetime, time, timezone

class TaskScheduler:
    def __init__(self, orchestrator, config):
        self.orchestrator = orchestrator
        self.config = config  # NightWindowConfig
        self._scheduled_tasks = {}  # task_id -> asyncio.Task
        self._running = False

    async def start(self):
        """Start the scheduler loop."""
        self._running = True
        while self._running:
            now = datetime.now(timezone.utc)
            if self._is_in_night_window(now):
                await self._process_pending_scheduled_tasks()
            await asyncio.sleep(60)  # Check every minute

    async def schedule_task(self, description, project_id, schedule_time=None, cron=None):
        """Schedule a task for later execution."""
        if schedule_time:
            delay = (schedule_time - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
        await self.orchestrator.submit_task(description, project_id)

    def _is_in_night_window(self, now):
        """Check if current time is within the configured night window."""
        # e.g., 22:00-06:00 UTC
        current_time = now.time()
        start = self.config.night_start  # time(22, 0)
        end = self.config.night_end      # time(6, 0)
        if start < end:
            return start <= current_time < end
        else:  # Window crosses midnight
            return current_time >= start or current_time < end
```

**Advantages**:
- Zero additional dependencies
- Full control over scheduling logic
- Native asyncio integration with existing Orchestrator
- Can implement night-window logic directly
- Can persist schedule state via the existing Rust engine (through PyO3)
- No infrastructure additions (no new message brokers)

**Disadvantages**:
- Must implement cron parsing manually (or use `croniter` library)
- Must implement persistence manually (store schedules in PostgreSQL via Rust engine)
- Must handle edge cases (DST transitions, missed executions on restart)
- No distributed coordination across multiple Python processes
- Single-process scheduler -- if the process crashes, scheduled tasks are lost until recovery

**Cron parsing**: The `croniter` library (1,600+ GitHub stars, stable) provides cron expression iteration in Python. It supports standard 5-field cron and timezone-aware scheduling.

**Persistence approach**:
- Store scheduled tasks in PostgreSQL via the Rust engine's `write_memory()` or a dedicated `scheduled_tasks` table
- On startup, load pending scheduled tasks and re-register them
- Record execution history in PostgreSQL

---

### Comparison Matrix

| Feature | APScheduler v4 | APScheduler v3 | Celery Beat | Custom asyncio |
|---|---|---|---|---|
| Cron expressions | Yes | Yes | Yes | Via croniter |
| Timezone support | Yes | Yes (pytz) | Yes | Manual (zoneinfo) |
| Async-native | Yes (asyncio/Trio) | Partial (bolt-on) | No (gevent/eventlet) | Yes |
| PostgreSQL persistence | Yes | Yes (SQLAlchemy) | Via django-celery-beat | Must implement |
| NATS integration | No | No | No (needs broker) | Via Rust engine |
| Multi-node coordination | Yes (event brokers) | No | Yes (via broker) | Must implement |
| Production readiness | Pre-release (alpha) | Stable | Stable | Custom code |
| Infrastructure overhead | Low (uses existing PG) | Low | High (needs broker) | None |
| Integration effort | Medium | Medium | High | Medium |

### Integration Considerations for UltimateCoders

1. **APScheduler v4** is the most feature-complete Python scheduling library but is explicitly not production-ready. The project should not depend on alpha software for a core scheduling feature.

2. **APScheduler v3** is stable but its async support is limited. It would require wrapping in an async adapter to work with the existing async Orchestrator.

3. **Celery Beat** is a poor fit due to broker incompatibility and sync-first design.

4. **Custom asyncio** with `croniter` is the most practical approach because:
   - The Python Agent layer already runs in an async context
   - Schedule persistence can go through the existing Rust engine (PyO3 -> PostgreSQL)
   - Night-window logic can be implemented directly
   - No new infrastructure dependencies
   - Full control over the scheduling lifecycle

5. **Hybrid approach**: The best architecture may be:
   - **Rust side**: Use `tokio-cron-scheduler` for the core scheduling engine (cron parsing, time management, persistence)
   - **Python side**: Custom thin async wrapper that:
     - Calls the Rust scheduler via PyO3 to register/query/cancel scheduled tasks
     - Receives callbacks when scheduled tasks fire
     - Delegates to `orchestrator.submit_task()` for execution
   - This keeps the scheduling logic in Rust (where it can be shared across gRPC nodes) while keeping the Python Agent layer simple.

### External References

- [APScheduler docs (v4)](https://apscheduler.readthedocs.io/en/master/) -- latest v4 documentation
- [APScheduler PyPI](https://pypi.org/project/APScheduler/) -- version history (latest stable: 3.11.2, latest v4: 4.0.0a6)
- [Celery documentation](https://docs.celeryq.dev/) -- Celery Beat scheduling
- [croniter PyPI](https://pypi.org/project/croniter/) -- cron expression parser for Python
- [Python zoneinfo](https://docs.python.org/3/library/zoneinfo.html) -- timezone handling (Python 3.9+, built-in)
- [asyncio task scheduling patterns](https://docs.python.org/3/library/asyncio-task.html) -- native async primitives

## Caveats / Not Found

- APScheduler v4's exact API surface for combining triggers was not verified from source code
- The interaction between APScheduler's PostgreSQL event broker and the project's existing PostgreSQL schema was not analyzed for conflicts
- Custom asyncio approach requires careful design for crash recovery -- the exact recovery mechanism was not fully specified
- The PyO3 bridge for calling Rust scheduler from Python would need to be designed; the existing `_uc_core` module may need new exposed functions
