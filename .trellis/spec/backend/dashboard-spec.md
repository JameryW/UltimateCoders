# Dashboard Code-Spec

> FastAPI + Jinja2 + SSE monitoring dashboard — executable contracts for implementation.

---

## Scenario: Real-time Cluster Monitoring Dashboard

### 1. Scope / Trigger

- Trigger: Any time the Orchestrator is running and `start_dashboard()` is called, the dashboard serves real-time cluster state.
- Cross-layer: Python Orchestrator in-memory state + Rust Engine health (PyO3) → FastAPI REST/SSE → Browser

### 2. Signatures

#### DashboardApp (`python/ultimate_coders/dashboard/app.py`)

```python
class DashboardApp:
    def __init__(self, orchestrator: Any) -> None
    def start(self, host: str = "0.0.0.0", port: int = 8080) -> None
    def stop(self) -> None

    # REST API endpoints (GET)
    GET /dashboard/              → HTMLResponse  (Jinja2 template)
    GET /dashboard/api/health    → JSONResponse  (engine health)
    GET /dashboard/api/workers   → JSONResponse  (worker list)
    GET /dashboard/api/tasks     → JSONResponse  (task status)
    GET /dashboard/api/scheduler → JSONResponse  (scheduler state)
    GET /dashboard/api/circuit-breaker → JSONResponse (CB + rate limiter)
    GET /dashboard/api/stream    → EventSourceResponse (SSE: hybrid real-time events + 5s snapshot)
    GET /dashboard/api/events    → JSONResponse  (event log, supports ?task_id=&limit=)

    # REST API endpoints (POST — interactive operations)
    POST /dashboard/api/tasks/submit              → JSONResponse (submit new task)
    POST /dashboard/api/tasks/{id}/pause          → JSONResponse (pause task)
    POST /dashboard/api/tasks/{id}/resume         → JSONResponse (resume task)
    POST /dashboard/api/circuit-breaker/reset     → JSONResponse (reset CB)
    POST /dashboard/api/scheduler/jobs/{id}/trigger → JSONResponse (trigger job)
    POST /dashboard/api/tasks/flush-pending       → JSONResponse (flush night-window queue)

    # Internal data collectors
    def _get_health_data(self) -> dict
    def _get_workers_data(self) -> dict
    def _get_tasks_data(self) -> dict
    def _get_scheduler_data(self) -> dict
    def _get_circuit_breaker_data(self, health_data: dict | None = None) -> dict
    def _get_full_snapshot(self) -> dict
    def _record_event(self, event_type: str, **details) -> None
```

#### TaskEventEmitter (`python/ultimate_coders/agent/event_emitter.py`)

```python
@dataclass
class TaskEvent:
    timestamp: str       # ISO 8601 UTC
    type: str            # event type (e.g., subtask_started, tool_call)
    task_id: str
    subtask_id: str      # optional
    data: dict[str, Any] # event-specific payload
    def to_dict(self) -> dict[str, Any]

class TaskEventEmitter:
    def __init__(self, buffer_size: int = 500) -> None
    async def emit(self, event_type: str, task_id: str = "", subtask_id: str = "", data: dict | None = None) -> None
    async def wait_for_event(self, timeout: float = 5.0) -> TaskEvent | None
    def get_recent_events(self, task_id: str | None = None, limit: int = 100) -> list[dict]
    @property
    def pending_count(self) -> int
```

#### Worker Event Integration (`python/ultimate_coders/agent/worker.py`)

```python
class Worker:
    def __init__(self, ..., event_emitter: TaskEventEmitter | None = None)
    # Emits: subtask_started, subtask_completed, subtask_failed, tool_call, tool_result
```

#### LLMClient Hook (`python/ultimate_coders/agent/llm.py`)

```python
class LLMClient:
    async def complete_with_tools(self, ..., on_tool_call: Callable | None = None)
    # on_tool_call signature: async (tool_name: str, tool_input: dict, result: str) -> None
```

#### Orchestrator Integration (`python/ultimate_coders/agent/orchestrator.py`)

```python
class Orchestrator:
    _dashboard_app: DashboardApp | None  # lazy init
    event_emitter: TaskEventEmitter      # created in __init__

    def start_dashboard(self, host: str = "0.0.0.0", port: int = 8080) -> None
    def stop_dashboard(self) -> None
    def pause_task(self, task_id: str) -> bool
    def resume_task(self, task_id: str) -> bool
    def reset_circuit_breaker(self) -> bool
    # Emits: task_submitted (in submit_task), task_completed (in handle_subtask_result)
```

#### CircuitBreaker (`python/ultimate_coders/agent/rate_limiter.py`)

```python
class CircuitBreaker:
    # Existing methods ...
    def reset(self) -> None  # Reset to CLOSED state, clear counts
```

#### Scheduler (`python/ultimate_coders/agent/scheduler.py`)

```python
class Scheduler:
    # Existing methods ...
    def trigger_job(self, task_id: str) -> bool  # Manually trigger a scheduled job
```

### 3. Contracts

#### POST Endpoint Contracts

| Endpoint | Success Response | Error Conditions |
|----------|-----------------|------------------|
| `POST /tasks/{id}/pause` | `{"success": true, "task_id": ..., "status": "paused"}` | 400: task not found or not pausable, 503: no orchestrator |
| `POST /tasks/{id}/resume` | `{"success": true, "task_id": ..., "status": "in_progress"}` | 400: task not found or not resumable, 503: no orchestrator |
| `POST /circuit-breaker/reset` | `{"success": true, "state": "closed"}` | 400: no CB configured, 503: no orchestrator |
| `POST /scheduler/jobs/{id}/trigger` | `{"success": true, "job_id": ...}` | 404: job not found, 503: no scheduler |
| `POST /tasks/flush-pending` | `{"success": true, "pending_count": N}` | 503: no orchestrator |
| `POST /tasks/submit` | `{"success": true, "task_id": ..., "status": ..., "subtask_count": N, "subtasks": [...]}` | 400: no description or invalid JSON, 503: no orchestrator |

#### Task Submit Contract

- **Request**: `{ "description": str (required), "project_id": str (optional) }`
- **Response (success)**: `{ "success": true, "task_id": str, "status": str, "subtask_count": int, "subtasks": [{ "id": str, "description": str, "status": str, "depends_on": list[str] }] }`
- **Response (error)**: `{ "success": false, "error": str }`
- **Behavior**: Calls `orchestrator.submit_task(description, project_id=...)`, which decomposes the task into subtasks and emits a `task_submitted` event via the emitter. The dashboard endpoint does NOT duplicate the emit — the Orchestrator is the single source of truth for task_submitted events.

#### Task Event Types

| Event | Emitter | Payload (`data`) |
|-------|---------|------------------|
| `task_submitted` | Orchestrator.submit_task | `{description, project_id, status, subtask_count}` |
| `task_completed` | Orchestrator.handle_subtask_result | `{status, subtask_count}` |
| `subtask_started` | Worker.execute_subtask | `{description, worker_id}` |
| `subtask_completed` | Worker.execute_subtask | `{summary, success, modified_files: [{path, type}]}` |
| `subtask_failed` | Worker.execute_subtask | `{error, worker_id}` |
| `tool_call` | Worker._on_tool_call | `{tool, input_summary}` |
| `tool_result` | Worker._on_tool_call | `{tool, result_summary}` |
| `llm_request` | Worker._execute_with_llm | `{model, messages_summary: [{role, content_preview}]}` |

#### SSE Hybrid Push Contract

SSE stream pushes two event types:
- **`task_event`** — pushed immediately when a TaskEvent is emitted via `event_emitter.wait_for_event()`. Payload is a single `TaskEvent.to_dict()` JSON.
- **`update`** — pushed every 5 seconds as a fallback (when no event_emitter, or when `wait_for_event()` times out). Payload is the full state snapshot JSON.

> **Gotcha**: When `event_emitter` is None (no Orchestrator, or old code path), the SSE loop must still `await asyncio.sleep(5)` after each snapshot to avoid an infinite fast-loop that would consume CPU and bandwidth.

#### Events API Contract

- `GET /dashboard/api/events?task_id=X&limit=N` — returns recent events
- Combines local `_event_log` deque with `event_emitter.get_recent_events()`
- `task_id` query param filters by `e.get("task_id") == task_id` OR `e.get("details", {}).get("task_id") == task_id` (covers both TaskEvent format and legacy `_record_event` format)
- Default `limit=100`

#### Event Log Contract

- In-memory ring buffer: `deque(maxlen=200)`, newest first (`appendleft`)
- Events recorded on every POST operation via `_record_event(event_type, **details)`
- Event structure: `{"timestamp": "...", "type": "event_type", "details": {...}}`
- Available via `GET /dashboard/api/events` and in SSE snapshot `events` field
- Events are lost on restart (no persistence)

#### SSE Event Format

Each SSE event payload is a JSON string with this structure:

```json
{
  "timestamp": "2026-06-12T08:30:00Z",
  "health": {
    "status": "ok|degraded|error",
    "version": "0.1.0",
    "uptime_seconds": 3600,
    "components": [
      {"name": "short_term_memory", "status": "ok", "details": "..."}
    ]
  },
  "workers": {
    "available": true,
    "workers": [
      {"id": "w1", "capabilities": ["code"], "current_load": 2, "max_capacity": 5, "last_heartbeat": "..."}
    ]
  },
  "tasks": {
    "available": true,
    "total": 10,
    "status_counts": {"in_progress": 3, "completed": 5, "failed": 2},
    "pending_task_count": 0,
    "tasks": [
      {"id": "...", "description": "...", "status": "in_progress", "project_id": "...", "subtask_count": 2, "created_at": "...", "updated_at": "..."}
    ]
  },
  "scheduler": {
    "available": true|false,
    "is_running": true,
    "night_window": {"start": "22:00", "end": "06:00", "timezone": "Asia/Shanghai", "is_active": false},
    "jobs": [...],
    "recent_executions": [...]
  },
  "circuit_breaker": {
    "available": true|false,
    "circuit_breaker": {"available": true|false, "state": "Closed|Open|HalfOpen|Unknown", "failure_count": 0, "total_calls": 100, "total_rejected": 0},
    "rate_limiter": {"available": true|false, "rpm_available": 60, "tpm_available": 100000, "active_count": 2, "total_requests": 15},
    "engine_circuit_breaker": {},
    "engine_rate_limiter": {}
  },
  "events": [
    {"timestamp": "2026-06-12T08:30:00Z", "type": "task_pause", "details": {"task_id": "..."}},
    {"timestamp": "2026-06-12T08:29:00Z", "type": "circuit_breaker_reset", "details": {}}
  ]
}
```

#### Fallback Contracts

| Condition | Panel Response |
|-----------|---------------|
| `orchestrator` is None | All panels: `{"available": false}` with full key structure (CB/RL include all metric keys with zero/Unknown defaults) |
| `orchestrator.scheduler` is None | `scheduler.available = false`, `night_window = null`, `jobs = []` |
| `orchestrator.engine` is None | `health.status = "unavailable"`, `components = []` |
| `engine.health()` raises exception | `health.status = "error"`, `error` key with message, `components = []` |
| `scheduler.list_jobs()` raises exception | `scheduler.available = true` (still running), `jobs = []` |
| `circuit_breaker` attribute missing or None | `circuit_breaker.available = false`, all metric keys present with zero/Unknown defaults |
| `rate_limiter` attribute missing or None | `rate_limiter.available = false`, all metric keys present with zero defaults |
| CB/RL read raises exception | `available = false`, `error` key added to default dict (dict not replaced) |
| No workers registered | `workers.available = true`, `workers = []` |
| No tasks | `tasks.available = true`, `tasks = []`, `total = 0` |

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| `start_dashboard()` called twice | Second call is a no-op (idempotent) |
| `stop_dashboard()` without start | No-op |
| Dashboard port already in use | `uvicorn` raises error, logged but doesn't crash Orchestrator |
| SSE client disconnects | `EventSourceResponse` handles gracefully, generator exits |
| `engine.health()` PyO3 call fails | Caught, returns `{"status": "error", "components": []}` |
| `scheduler.check_night_window()` fails | Caught, returns `night_window = null` |

### 5. Good/Base/Bad Cases

**Good**: Full stack running with scheduler
```python
engine = Engine()
scheduler = Scheduler()
orch = Orchestrator(scheduler=scheduler, engine=engine)
orch.start_dashboard(port=8080)
# → All panels populated, SSE pushing every 5s
```

**Base**: No scheduler, no infrastructure
```python
orch = Orchestrator()
orch.start_dashboard(port=8080)
# → Health: degraded/error, Workers: empty, Tasks: empty, Scheduler: "Not Available"
```

**Bad**: Dashboard port conflict
```python
orch.start_dashboard(port=80)  # privileged port
# → uvicorn bind error logged, dashboard doesn't start, Orchestrator unaffected
```

### 6. Tests Required

| Test | Type | Assertion |
|------|------|-----------|
| Dashboard page returns HTML | Unit | Status 200, content-type text/html |
| Health API returns JSON | Unit | `status` key present in response |
| Workers API returns JSON | Unit | `available` and `workers` keys |
| Tasks API returns JSON | Unit | `available`, `total`, `by_status` keys |
| Scheduler API without scheduler | Unit | `available = false` |
| Scheduler API with scheduler | Unit | `available = true`, `jobs` is list |
| Circuit breaker without engine | Unit | `circuit_breaker.available = false`, structure has all keys |
| SSE stream route registered | Unit | Route `/dashboard/api/stream` exists |
| Full snapshot is JSON-serializable | Unit | `json.dumps(snapshot)` succeeds |
| Orchestrator.start_dashboard() | Unit | Creates DashboardApp, starts uvicorn in thread |
| Orchestrator.stop_dashboard() | Unit | Sets `should_exit`, joins thread |
| Fallback: no orchestrator | Unit | All panels return `available: false` with consistent structure |
| Fallback: no engine | Unit | Health returns `status: "unavailable"` |
| start_dashboard idempotent | Unit | Second call is no-op |

### 6b. Tests Required (Interactive + Event Log)

| Test | Type | Assertion |
|------|------|-----------|
| POST pause task | Unit | Status 200, `success=true`, `status=paused` |
| POST pause not found | Unit | Status 400 |
| POST resume task | Unit | Status 200, `success=true`, `status=in_progress` |
| POST resume not paused | Unit | Status 400 |
| POST CB reset | Unit | Status 200, `success=true`, `state=closed` |
| POST CB reset no CB | Unit | Status 400 |
| POST flush pending | Unit | Status 200, `success=true`, has `pending_count` |
| POST trigger job no scheduler | Unit | Status 503 |
| POST pause no orchestrator | Unit | Status 503 |
| _record_event appends | Unit | Event log has entry, newest first |
| Event log maxlen | Unit | Bounded at 200 entries |
| GET events endpoint | Unit | Returns event list with `available=true` |
| Snapshot includes events | Unit | `events` key present in full snapshot |
| Orchestrator.pause_task | Unit | Returns True, task status = PAUSED |
| Orchestrator.resume_task | Unit | Returns True, task status = IN_PROGRESS |
| Orchestrator.reset_circuit_breaker | Unit | Returns True, CB state = CLOSED |
| CircuitBreaker.reset from OPEN | Unit | State transitions to CLOSED, counts zeroed |
| CircuitBreaker.reset from HALF_OPEN | Unit | State transitions to CLOSED |

### 6c. Tests Required (Task Submit + Event Emitter)

| Test | Type | Assertion |
|------|------|-----------|
| POST /tasks/submit success | Unit | Status 200, `success=true`, has `task_id` |
| POST /tasks/submit no description | Unit | Status 400 |
| POST /tasks/submit no orchestrator | Unit | Status 503 |
| POST /tasks/submit invalid JSON | Unit | Status 400 |
| TaskEventEmitter emit + get_recent | Unit | Buffer has 1 event with correct type/task_id |
| TaskEventEmitter get_recent filtered | Unit | Returns only events for given task_id |
| TaskEventEmitter buffer maxlen | Unit | Bounded at buffer_size |
| TaskEvent.to_dict serialization | Unit | Includes type, task_id, subtask_id (if set), timestamp |
| Orchestrator has event_emitter | Unit | isinstance TaskEventEmitter |
| Orchestrator submit emits task_submitted | Unit | get_recent_events returns task_submitted event |
| Worker emits subtask_started | Unit | Event with type=subtask_started in buffer |
| Worker emits subtask_completed | Unit | Event with type=subtask_completed, data.modified_files |
| Worker emits subtask_failed | Unit | Event with type=subtask_failed on exception |
| Worker emits llm_request | Unit | Event with type=llm_request, data has model and messages_summary |
| Worker backward compat (no emitter) | Unit | Worker works without event_emitter, no errors |
| task_completed emitted on all success | Unit | Orchestrator.handle_subtask_result emits task_completed |
| task_completed emitted on all failed | Unit | Orchestrator.handle_subtask_result emits task_completed |
| Events API task_id filter | Unit | GET /events?task_id=X returns only events for that task |
| Events API no filter | Unit | GET /events returns all events |

### 7. Wrong vs Correct

#### Wrong: Duplicate engine.health() calls in SSE snapshot

```python
# BAD: health() called twice per SSE push (PyO3 → Rust round-trip)
health_data = self._get_health_data()
cb_data = self._get_circuit_breaker_data()  # calls health() again internally
```

#### Correct: Pass health_data to avoid duplicate PyO3 calls

```python
# GOOD: health() called once, result passed to circuit breaker
health_data = self._get_health_data()
cb_data = self._get_circuit_breaker_data(health_data=health_data)
```

#### Wrong: Inconsistent response structure for missing orchestrator

```python
# BAD: missing keys break JS frontend
def _get_circuit_breaker_data(self):
    if not self.orchestrator:
        return {"available": False}  # no circuit_breaker or rate_limiter keys!

# BAD: sparse dict on exception overwrites all defaults
cb_data = {"available": False}
try:
    cb_data = read_cb()
except Exception:
    cb_data = {"available": False, "error": str(e)}  # lost state/failure_count keys!
```

#### Correct: Always include all expected keys, preserve defaults on error

```python
# GOOD: consistent structure, JS can always read data["circuit_breaker"]["state"]
def _get_circuit_breaker_data(self):
    if not self.orchestrator:
        return {
            "available": False,
            "circuit_breaker": {"available": False, "state": "Unknown", "failure_count": 0, ...},
            "rate_limiter": {"available": False, "rpm_available": 0, "tpm_available": 0, ...},
            ...
        }

# GOOD: on exception, add error to existing defaults instead of replacing
cb_data = {"available": False, "state": "Unknown", "failure_count": 0, ...}
try:
    cb_data = read_cb()
except Exception as e:
    cb_data["error"] = str(e)  # preserves all default keys
```

#### Wrong: Duplicate task_submitted event emission

```python
# BAD: Both Orchestrator.submit_task() and the dashboard endpoint emit task_submitted
# This causes two SSE events per task submission
@app.post("/dashboard/api/tasks/submit")
async def submit_task_api(request):
    task = await orch.submit_task(description)  # already emits task_submitted inside
    await self.event_emitter.emit("task_submitted", task_id=task.id, ...)  # DUPLICATE!
```

#### Correct: Single source of truth for event emission

```python
# GOOD: Only Orchestrator emits task_submitted; dashboard endpoint just records locally
@app.post("/dashboard/api/tasks/submit")
async def submit_task_api(request):
    task = await orch.submit_task(description)  # emits task_submitted
    self._record_event("task_submitted", task_id=task.id, description=description)  # local log only
```

#### Wrong: SSE infinite fast-loop when event_emitter is None

```python
# BAD: No sleep when event_emitter is None → CPU/bandwidth hot loop
async def event_generator():
    while True:
        if self.event_emitter is not None:
            event = await self.event_emitter.wait_for_event(timeout=5.0)
            if event is not None:
                yield {"event": "task_event", "data": json.dumps(event.to_dict())}
                continue
        # No event_emitter and no sleep → infinite tight loop!
        snapshot = self._get_full_snapshot()
        yield {"event": "update", "data": json.dumps(snapshot)}
```

#### Correct: Always sleep after snapshot when no event received

```python
# GOOD: asyncio.sleep(5) prevents hot loop when event_emitter is None or on timeout
async def event_generator():
    while True:
        if self.event_emitter is not None:
            event = await self.event_emitter.wait_for_event(timeout=5.0)
            if event is not None:
                yield {"event": "task_event", "data": json.dumps(event.to_dict())}
                continue
        # Timeout or no emitter: send snapshot + sleep
        snapshot = self._get_full_snapshot()
        yield {"event": "update", "data": json.dumps(snapshot)}
        await asyncio.sleep(5)
```

#### Wrong: Mutating cached event objects in frontend

```javascript
// BAD: Directly mutating _interactionLog event objects causes state persistence bugs
_interactionLog[tid].forEach(ev => {
  ev.data.modified_files.forEach(mf => {
    mf._source_subtask = ev.subtask_id;  // mutates original cached object!
  });
});
```

#### Correct: Create new objects instead of mutating cached ones

```javascript
// GOOD: Create a new plain object to avoid side-effects on cached event data
const fileEntry = {path: mf.path, type: mf.type, _source_subtask: ev.subtask_id};
```

---

## Architecture

```
Browser ──SSE──> FastAPI (/dashboard/api/stream)
              ──GET──> FastAPI (/dashboard/api/*)
              ──POST─> FastAPI (/dashboard/api/tasks/submit, etc.)
                          │
                     Orchestrator (embedded)
                          │
                ┌─────────┼──────────┐
                │         │          │
          Engine.health()  workers   Scheduler
          (PyO3/Rust)     tasks     jobs/history

Event flow:
  Worker ──emit()──> TaskEventEmitter ──await──> SSE (task_event)
  Orchestrator ──emit()──> TaskEventEmitter
                                        └──timeout──> SSE (update, 5s full snapshot)
```

- Dashboard runs in a **background thread** via `uvicorn.Server.run()`
- Orchestrator is **not blocked** by dashboard I/O
- `stop_dashboard()` sets `server.should_exit = True` and joins the thread
- **CORS middleware** enabled (`allow_origins=["*"]`, `allow_methods=["GET"]`) for CDN script loading (Tailwind) and cross-origin SSE clients
- **TaskEventEmitter** is an in-process asyncio.Queue + ring buffer. Workers and Orchestrator `emit()` events; Dashboard SSE `wait_for_event()` consumes them. Events are not persisted — lost on restart.
- **SSE hybrid push**: real-time `task_event` SSE events for Worker/Orchestrator events, plus periodic `update` SSE events (5s full snapshot) as fallback
- **Backward compatibility**: `event_emitter` and `on_tool_call` are optional. Without them, Worker and LLMClient behave identically to pre-event code paths.

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | >=0.100 | REST API framework |
| `uvicorn` | >=0.23 | ASGI server |
| `jinja2` | >=3.1 | HTML template rendering |
| `sse-starlette` | >=1.6 | Server-Sent Events |
| `httpx` | >=0.24 | Test client (test dependency) |

---

## UI Conventions

- Tailwind CSS via CDN (no node/npm build step)
  > **Gotcha**: Do NOT add `crossorigin="anonymous"` to the Tailwind CDN `<script>` tag. The play CDN (`cdn.tailwindcss.com`) does not support CORS credentials — the attribute causes fetch failures.
- Mermaid.js via CDN (`cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js`) for subtask DAG rendering
  > **Note**: SRI not applicable for Mermaid CDN (version-pinned by URL, similar to Tailwind play CDN)
- Dark theme: `bg-[#0f172a]` body, `bg-[#1e293b]` cards, `border-[#334155]`
- 4-column grid: `grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4`
- Status colors: green (`text-green-400` / `bg-green-900`), yellow (`text-yellow-400`), red (`text-red-400`)
- Status badges: `badge-ok`, `badge-degraded`, `badge-error`, `badge-closed`, `badge-open`, `badge-half_open`, `badge-unavailable`
- SSE auto-reconnect: `new EventSource('/dashboard/api/stream')` (built-in browser reconnect)
- Connection indicator: `pulse-dot` CSS animation in header (green = connected, red = disconnected)
- Initial data fetch: `fetchInitialData()` calls REST endpoints for faster first paint before SSE connects
- Confirm modal: custom dark-theme modal (`modal-overlay` + `modal-box`) for all POST operations
- Toast notifications: `toast-success` (green) / `toast-error` (red), auto-dismiss after 4s
- Action buttons: `btn-action` base class + `btn-pause` / `btn-resume` / `btn-danger` / `btn-trigger` / `btn-flush` variants
- Task detail expansion: click task row → toggle `task-detail.expanded`, load subtask list + interaction log + output files + Mermaid DAG
- Event Log panel: newest-first list, color-coded by event type
- Task submit form: `textarea` (description) + `input` (project_id, optional) + submit button, POST to `/dashboard/api/tasks/submit`
- Interaction log: per-task event stream in detail expansion, color-coded entries (blue=tool_call, green=tool_result/subtask_completed, yellow=subtask_started, red=subtask_failed)
- Output files: shown in task detail from `subtask_completed` events with `modified_files` data
- Mermaid DAG: auto-generated from subtask `depends_on` relationships, rendered via `mermaid.render()`
- SSE event handling: `task_event` SSE type → `handleTaskEvent()` → append to `_interactionLog` + event log panel + live detail update
