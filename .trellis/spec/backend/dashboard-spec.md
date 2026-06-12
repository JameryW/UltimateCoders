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

    # REST API endpoints
    GET /dashboard/              → HTMLResponse  (Jinja2 template)
    GET /dashboard/api/health    → JSONResponse  (engine health)
    GET /dashboard/api/workers   → JSONResponse  (worker list)
    GET /dashboard/api/tasks     → JSONResponse  (task status)
    GET /dashboard/api/scheduler → JSONResponse  (scheduler state)
    GET /dashboard/api/circuit-breaker → JSONResponse (CB + rate limiter)
    GET /dashboard/api/stream    → EventSourceResponse (SSE every 5s)

    # Internal data collectors
    def _get_health_data(self) -> dict
    def _get_workers_data(self) -> dict
    def _get_tasks_data(self) -> dict
    def _get_scheduler_data(self) -> dict
    def _get_circuit_breaker_data(self, health_data: dict | None = None) -> dict
    def _get_full_snapshot(self) -> dict
```

#### Orchestrator Integration (`python/ultimate_coders/agent/orchestrator.py`)

```python
class Orchestrator:
    _dashboard_app: DashboardApp | None  # lazy init

    def start_dashboard(self, host: str = "0.0.0.0", port: int = 8080) -> None
    def stop_dashboard(self) -> None
```

### 3. Contracts

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
  }
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

---

## Architecture

```
Browser ──SSE──> FastAPI (/dashboard/api/stream)
              ──GET──> FastAPI (/dashboard/api/*)
                          │
                     Orchestrator (embedded)
                          │
                ┌─────────┼──────────┐
                │         │          │
          Engine.health()  workers   Scheduler
          (PyO3/Rust)     tasks     jobs/history
```

- Dashboard runs in a **background thread** via `uvicorn.Server.run()`
- Orchestrator is **not blocked** by dashboard I/O
- `stop_dashboard()` sets `server.should_exit = True` and joins the thread
- **CORS middleware** enabled (`allow_origins=["*"]`, `allow_methods=["GET"]`) for CDN script loading (Tailwind) and cross-origin SSE clients

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
- Dark theme: `bg-[#0f172a]` body, `bg-[#1e293b]` cards, `border-[#334155]`
- 4-column grid: `grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4`
- Status colors: green (`text-green-400` / `bg-green-900`), yellow (`text-yellow-400`), red (`text-red-400`)
- Status badges: `badge-ok`, `badge-degraded`, `badge-error`, `badge-closed`, `badge-open`, `badge-half_open`, `badge-unavailable`
- SSE auto-reconnect: `new EventSource('/dashboard/api/stream')` (built-in browser reconnect)
- Connection indicator: `pulse-dot` CSS animation in header (green = connected, red = disconnected)
- Initial data fetch: `fetchInitialData()` calls REST endpoints for faster first paint before SSE connects
