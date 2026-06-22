# NATS Bridge Spec

> Executable contracts for the NATS-based bridge between Rust gRPC server and Python Orchestrator.

---

## 1. Scope / Trigger

- Trigger: TUI → gRPC → Python Orchestrator bidirectional task execution
- Cross-layer: Rust gRPC server ↔ NATS JetStream ↔ Python NATS consumer
- Requires code-spec depth because it defines cross-language message contracts, subject naming, payload formats, and failure behavior

---

## 2. Signatures

### NATS Subjects

| Subject | Direction | Purpose |
|---------|-----------|---------|
| `uc.task.submit` | gRPC/Dashboard → Python | New task submission |
| `uc.task.update` | Python → gRPC | Task/subtask status update |
| `uc.task.event` | Python → gRPC, gRPC → Python | Real-time execution event + pause/resume status change |
| `uc.heartbeat` | Python → gRPC | Consumer heartbeat |

### Rust NATS Protocol Types (`crates/uc-grpc/src/server.rs`)

```rust
// Publish payload (gRPC → Python)
pub struct NatsTaskSubmit {
    pub task_id: String,
    pub description: String,
    pub project_id: String,
}

// Receive payload (Python → gRPC)
pub struct NatsTaskUpdate {
    pub task_id: String,
    pub status: String,
    pub subtasks: Vec<NatsSubtaskUpdate>,
    pub result: Option<String>,
}

pub struct NatsSubtaskUpdate {
    pub id: String,
    pub description: String,
    pub status: String,
    pub assigned_worker: Option<String>,
    pub depends_on: Vec<String>,
}

pub struct NatsTaskEvent {
    pub event_type: String,
    pub task_id: String,
    pub subtask_id: Option<String>,
    pub data: HashMap<String, String>,
}

pub struct NatsHeartbeat {
    pub consumer_id: String,
    pub timestamp: String,
}
```

### Python NATS Publisher (`python/ultimate_coders/nats_worker.py`)

```python
class NatsPublisher:
    async def publish_update(self, task: Task) -> None
    async def publish_event(self, event_type: str, task_id: str, ...) -> None
    async def publish_heartbeat(self) -> None
    async def publish_submit(self, task_id: str, description: str, project_id: str = "") -> None
```

---

## 3. Contracts

### NATS Subject Constants

| Constant | Value | Defined In |
|----------|-------|------------|
| `NATS_SUBJECT_TASK_SUBMIT` | `"uc.task.submit"` | `crates/uc-grpc/src/server.rs` |
| `NATS_SUBJECT_TASK_UPDATE` | `"uc.task.update"` | `crates/uc-grpc/src/server.rs` |
| `NATS_SUBJECT_TASK_EVENT` | `"uc.task.event"` | `crates/uc-grpc/src/server.rs` |
| `NATS_SUBJECT_HEARTBEAT` | `"uc.heartbeat"` | `crates/uc-grpc/src/server.rs` |

### Environment Keys

| Key | Required | Default | Purpose |
|-----|----------|---------|---------|
| `UC_NATS_URL` | No | `nats://localhost:4222` | NATS server URL (Rust + Python) |
| `UC_SANDBOX_MODE` | No | — | Use sandbox decomposition in nats_worker |
| `UC_PROJECT_PATH` | No | `.` | Project path for sandbox/worker |
| `UC_GRPC_ADDR` | No | `[::]:50051` | gRPC server listen address |

### Task Status String Mapping (Rust ↔ Python)

| Rust `TaskStatus` | JSON string | Python `TaskStatus` |
|-------------------|-------------|---------------------|
| `Created` | `"Created"` | — |
| `Planning` | `"Planning"` | `PLANNING` |
| `InProgress` | `"InProgress"` | `IN_PROGRESS` |
| `Paused` | `"Paused"` | `PAUSED` |
| `Completed` | `"Completed"` | `COMPLETED` |
| `Failed` | `"Failed"` | `FAILED` |

### Subtask Status String Mapping

| Rust `SubtaskStatus` | JSON string | Python `SubtaskStatus` |
|----------------------|-------------|------------------------|
| `Pending` | `"Pending"` | `PENDING` |
| `Assigned` | `"Assigned"` | `ASSIGNED` |
| `InProgress` | `"InProgress"` | `IN_PROGRESS` |
| `Completed` | `"Completed"` | `COMPLETED` |
| `Failed` | `"Failed"` | `FAILED` |
| `Conflicted` | `"Conflicted"` | `CONFLICTED` |

### Event Type Mapping

| Python event_type | Rust `AgentEventType` variant | Direction |
|-------------------|-------------------------------|-----------|
| `task_submitted` | `TaskCreated` | Python → gRPC |
| `task_paused` | `TaskPaused` | gRPC → Python |
| `task_resumed` | `TaskResumed` | gRPC → Python |
| `subtask_assigned` | `SubtaskAssigned` | Python → gRPC |
| `subtask_completed` | — (via `uc.task.update`) | — |
| `subtask_failed` | — (via `uc.task.update`) | — |
| `task_completed` | — (via `uc.task.update`) | — |
| `tool_call` | — (stored in event log) | Python → gRPC |
| `llm_request` | — (stored in event log) | Python → gRPC |

### Pause/Resume Loop Prevention

When gRPC publishes `task_paused`/`task_resumed` via `uc.task.event`:

1. **Rust dedup**: `publish_task_status_event` pre-registers `message_id` in TaskStore's dedup map before NATS publish, so the NATS subscriber's `check_and_record_message_id()` skips the echo
2. **Python `_local` methods**: `Orchestrator.pause_task_local()`/`resume_task_local()` update local state only — no `engine.pause_task()` call, no NATS publish
3. **Idempotent guard**: If task is already in target state (e.g. already Paused when `task_paused` arrives), `_local` method returns False and does nothing

---

## 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| NATS unavailable at gRPC startup | `GrpcServer::with_nats()` logs warning, server starts without NATS, uses local TaskStore decomposition |
| NATS publish fails in `submit_task()` | Fallback to local newline-split decomposition, delete Planning placeholder task |
| NATS subscriber receives malformed JSON | Log warning, skip message, no crash |
| NATS subscriber receives unknown task_id | Log warning, skip update |
| NATS subscriber receives unknown status string | Preserve existing status, log warning |
| Python consumer crash | No heartbeat → heartbeat monitor marks InProgress tasks as Failed after timeout |
| Heartbeat timeout (default 10 min) | `TaskStore::mark_stale_tasks_failed()` marks all InProgress/Planning tasks as Failed |

---

## 5. Good/Base/Bad Cases

### Submit Task

- **Good**: NATS available → publish → Python decomposes via LLM → Worker executes → update → TUI sees Completed
- **Base**: NATS unavailable → local newline decomposition → task created immediately but no real execution
- **Bad**: NATS publish partially fails (task created in Planning but no consumer) → heartbeat timeout marks it Failed

### Watch Task Events

- **Good**: Python publishes events → gRPC subscriber pushes to TaskStore → WatchTask streams to TUI
- **Base**: No events from Python → WatchTask polls empty event log every 500ms
- **Bad**: Event JSON has wrong format → gRPC logs warning, skips event, TUI misses it

---

## 6. Tests Required

### Rust Tests (`crates/uc-grpc/src/server.rs`)

| Test | Assertion |
|------|-----------|
| `nats_task_submit_serialization` | NatsTaskSubmit JSON roundtrip |
| `nats_task_update_serialization` | NatsTaskUpdate JSON roundtrip |
| `nats_task_event_serialization` | NatsTaskEvent JSON roundtrip |
| `nats_heartbeat_serialization` | NatsHeartbeat JSON roundtrip |
| `task_status_from_str_roundtrip` | All 6 status strings roundtrip |
| `subtask_status_from_str_roundtrip` | All 6 subtask status strings roundtrip |
| `task_store_submit_pending` | submit_task_pending creates Planning task |
| `task_store_apply_update_existing_task` | apply_update updates task + adds subtasks |
| `task_store_apply_update_unknown_task` | Unknown task_id ignored |
| `task_store_apply_update_unknown_status` | Unknown status ignored |
| `task_store_mark_stale_tasks_with_heartbeat` | Heartbeat timeout → task Failed |
| `task_store_mark_stale_skips_completed_tasks` | Completed tasks not affected |
| `json_bool_or_default_true` | JSON bool `true` parsed correctly |
| `json_bool_or_default_false` | JSON bool `false` parsed correctly |
| `json_bool_or_default_string` | String "true"/"false" fallback |

### Python Tests (`tests/python/test_nats_worker.py`, `tests/python/test_dashboard.py`)

| Test | Assertion |
|------|-----------|
| `test_nats_task_submit_payload` | Submit payload format matches protocol |
| `test_nats_task_update_payload` | Update payload format matches protocol |
| `test_nats_heartbeat_payload` | Heartbeat payload format matches protocol |
| `test_publish_update_failure_graceful` | Publish failure logged, not raised |
| `test_publish_submit_payload` | Dashboard submit via NATS payload |
| `test_dashboard_nats_submit_fallback` | NATS fail → direct Orchestrator call |
| `test_dashboard_nats_event_handling` | Valid/invalid event payloads |

---

## 7. Wrong vs Correct

### Wrong: Holding Mutex across async NATS publish

```rust
// Wrong — blocks all TaskStore operations during publish
let mut store = self.inner.task_store.lock().await;
store.submit_task_pending(description.clone(), project_id.clone());
// Mutex held across .await point!
if let Some(client) = &self.inner.nats_client {
    let _ = client.publish(subject, payload.into()).await;
}
```

#### Correct

```rust
// Correct — extract data, release mutex, then publish
let (task_id, description, project_id) = {
    let mut store = self.inner.task_store.lock().await;
    let task = store.submit_task_pending(description, project_id);
    (task.id.0.clone(), description, project_id)
}; // Mutex released here
if let Some(client) = &self.inner.nats_client {
    let payload = serde_json::to_vec(&NatsTaskSubmit { ... }).unwrap();
    if client.publish(subject, payload.into()).await.is_err() {
        // Fallback: re-acquire mutex, delete Planning task, use local decomposition
    }
}
```

### Wrong: Parsing JSON boolean fields as strings

```rust
// Wrong — Python sends JSON bool, .as_str() returns None for bools
let success = data.get("success").and_then(|v| v.as_str())
    .map(|s| s == "true").unwrap_or(default);
```

#### Correct

```rust
// Correct — handle both JSON bool and string representations
fn json_bool_or_default(value: &serde_json::Value, default: bool) -> bool {
    match value {
        serde_json::Value::Bool(b) => *b,
        serde_json::Value::String(s) => s == "true",
        _ => default,
    }
}
```

### Wrong: Using deprecated `asyncio.get_event_loop()` from non-async context

```python
# Wrong — deprecated in Python 3.10+, doesn't access uvicorn's loop
def _schedule_subscribe(self):
    loop = asyncio.get_event_loop()
    loop.create_task(self._subscribe_nats_events())
```

#### Correct

```python
# Correct — use FastAPI startup event which runs on uvicorn's event loop
self._app.add_event_handler("startup", self._subscribe_nats_events)
```

---

## Known Limitations

1. **Task ID mismatch**: Orchestrator creates its own task_id, ignoring the one from NATS submit. The gRPC TaskStore's placeholder task stays in Planning forever. Fix requires Orchestrator accepting external task IDs.
2. **Subtask result type mismatch**: Python sends `result` as `Option<String>`, Rust expects `Option<SubtaskResult>` (a struct). Summary is silently discarded.
3. **No JetStream persistence**: Core NATS only (at-most-once delivery). If subscriber disconnects, updates are lost.
4. **No auto-reconnect**: If NATS connection breaks, gRPC subscriber exits. Requires server restart.
5. **Heartbeat is coarse**: 30-second heartbeat, 10-minute timeout. No per-task progress tracking.

---

## Architecture

```
TUI (Ink/React)
    ↓ gRPC (PauseTask/ResumeTask)
Rust gRPC Server ←→ NATS JetStream ←→ Python nats_worker
    ↑ TaskStore            ↑  ↓              ↓
    │ (subscribe)     (publish) (subscribe)  Orchestrator + Worker
    │                                     ↓
    └─── uc.task.update ◄──────── NATS ◄──┘
    └─── uc.task.event  ◄──────── NATS ◄──┘  (Python → gRPC)
    └─── uc.task.event  ────────► NATS ──►  (gRPC → Python: task_paused/task_resumed)
    └─── uc.heartbeat   ◄──────── NATS ◄──┘

Dashboard (FastAPI)
    ↓ uc.task.submit (NATS)
    → Python nats_worker (same as above)
```
