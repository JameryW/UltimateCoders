# Local Worker Bridge Spec

> Executable contracts for the JSON-RPC 2.0 bridge between Rust gRPC server and Python local_worker subprocess.

---

## 1. Scope / Trigger

- Trigger: TUI → gRPC → Python Orchestrator bidirectional task execution **without NATS**
- Cross-layer: Rust gRPC server ↔ stdin/stdout JSON-RPC ↔ Python local_worker
- Requires code-spec depth because it defines cross-language message contracts, JSON-RPC protocol, payload formats, and failure/degradation behavior

---

## 2. Signatures

### Rust (`crates/uc-grpc/src/local_worker.rs`)

```rust
pub struct LocalWorkerBridge {
    pub async fn spawn() -> Result<Self, String>
    pub fn is_available(&self) -> bool
    pub async fn ping(&self) -> Result<(), String>
    pub async fn submit_task(&self, description: &str, project_id: &str)
        -> Result<(WorkerTaskUpdate, Vec<WorkerTaskUpdate>), String>
    pub async fn kill(&self)
}

pub struct WorkerTaskUpdate {
    pub task_id: String,
    pub description: String,
    pub project_id: String,
    pub status: String,
    pub subtasks: Vec<WorkerSubtaskUpdate>,
    pub result: Option<String>,
}

pub struct WorkerSubtaskUpdate {
    pub id: String,
    pub description: String,
    pub status: String,
    pub assigned_worker: Option<String>,
    pub depends_on: Vec<String>,
}
```

### Python (`python/ultimate_coders/local_worker.py`)

```python
class LocalWorker:
    async def start(self) -> None
    async def _handle_message(self, raw: str) -> None
    async def _handle_submit(self, id_, params) -> None
    async def _execute_subtasks(self, task) -> None

class JsonRpcWriter:
    def write_response(self, id, result) -> None
    def write_notification(self, method, params) -> None
    def write_error(self, id, code, message) -> None
```

### GrpcServer routing (`crates/uc-grpc/src/server.rs`)

```rust
submit_task:
  NATS available?  → publish uc.task.submit (existing)
  local_worker available? → JSON-RPC submit_task (new)
  fallback → newline-split decomposition (existing)

GrpcServerInner.local_worker: Option<LocalWorkerBridge>
```

---

## 3. Contracts

### JSON-RPC Protocol (newline-delimited over stdin/stdout)

**Every message is one JSON line terminated by `\n`.**

#### Server → Worker: ping

```json
{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
```

#### Worker → Server: ping response

```json
{"jsonrpc": "2.0", "id": 1, "result": {"status": "ok"}}
```

#### Server → Worker: submit_task

```json
{"jsonrpc": "2.0", "id": 2, "method": "submit_task", "params": {"description": "Fix the bug", "project_id": "proj-1"}}
```

#### Worker → Server: task_update notification (may arrive zero or more times)

```json
{"jsonrpc": "2.0", "method": "task_update", "params": {"task_id": "t-1", "description": "Fix the bug", "project_id": "proj-1", "status": "in_progress", "subtasks": [{"id": "s-1", "description": "Write test", "status": "assigned", "assigned_worker": "w-1", "depends_on": []}], "result": null}}
```

#### Worker → Server: submit_task response (final)

```json
{"jsonrpc": "2.0", "id": 2, "result": {"task_id": "t-1", "description": "Fix the bug", "project_id": "proj-1", "status": "completed", "subtasks": [...], "result": "All subtasks completed"}}
```

#### Worker → Server: error response

```json
{"jsonrpc": "2.0", "id": 2, "error": {"code": -32001, "message": "Decomposition failed: ..."}}
```

### Error Codes

| Code | Meaning |
|------|---------|
| -32700 | Parse error (invalid JSON) |
| -32600 | Invalid request |
| -32601 | Method not found |
| -32602 | Invalid params |
| -32000 | Orchestrator not initialized |
| -32001 | Decomposition failed |
| -32002 | Execution failed |

### Environment Variables

| Key | Required | Default | Purpose |
|-----|----------|---------|---------|
| `UC_SANDBOX_MODE` | No | `""` | Set to `"subprocess"` for sandbox execution |
| `UC_PROJECT_PATH` | No | `os.getcwd()` | Project path for sandbox worker |

### Health Check

- `LocalWorkerBridge::spawn()` sends `ping` on startup
- 5-second timeout for health check
- Failure → `is_available() == false` → gRPC server falls back to newline-split

---

## 4. Validation & Error Matrix

| Condition | Error / Behavior |
|-----------|-----------------|
| Python not installed | `spawn()` returns `Err("Failed to spawn local_worker: ...")` |
| maturin not built | `spawn()` succeeds, `ping()` fails with timeout → `Err("Worker health check timed out (5s)")` |
| Worker crashes mid-task | `submit_task()` returns `Err("Worker process exited (stdout closed)")` |
| Worker sends invalid JSON | Rust side silently skips the line (logged as debug) |
| Empty description | Worker returns `error: {code: -32602, message: "Empty description"}` |
| LLM client not configured | Orchestrator `decompose_task()` raises → Worker returns `error: {code: -32001}` |
| NATS also unavailable | Falls through to newline-split decomposition |

---

## 5. Good/Base/Bad Cases

**Good**: gRPC server starts → local_worker.py spawns → ping OK → submit_task → subtasks assigned/executed → task_update notifications → completed

**Base**: gRPC server starts → local_worker.py spawn fails (no Python) → falls back to newline-split → tasks created but not executed

**Bad**: local_worker.py starts → ping OK → submit_task → worker crashes mid-execution → Rust detects stdout closed → marks worker unavailable → falls back to newline-split

---

## 6. Tests Required

| Test | Assertion Points |
|------|-----------------|
| `worker_task_update_deserialize` | JSON roundtrip, field access |
| `json_rpc_request_serialize` | Method name present, no newlines |
| `submit_task_fallback_without_worker` | Server created even without worker |
| `json_rpc_protocol_format` | All message types are single-line |
| `apply_worker_update_to_task_store` | WorkerTaskUpdate → TaskStore correctly |
| Integration: spawn + ping | Worker responds to health check |
| Integration: submit_task end-to-end | Subtask status flow completed |

---

## 7. Wrong vs Correct

### Wrong: Hold task_store lock across async JSON-RPC call

```rust
// BAD — blocks all TaskStore operations for the entire worker execution
let mut store = self.inner.task_store.lock().await;
let result = worker.submit_task(&req.description, &req.project_id).await?;
store.tasks.insert(result.task_id, task);
```

### Correct: Apply updates after JSON-RPC completes

```rust
// GOOD — lock only when updating the store
let (final_update, notifications) = worker.submit_task(&req.description, &req.project_id).await?;
for notif in &notifications {
    self.apply_worker_update(notif).await;  // acquires lock briefly
}
self.apply_worker_update(&final_update).await;
```

---

## Design Decision: Long-lived worker vs per-task spawn

**Context**: Need local task execution without NATS.

**Options**:
1. Per-task spawn — simple, but Python startup 1-3s + maturin load
2. Long-lived worker (chosen) — one spawn, reuse for all tasks
3. PyO3 in-process — GIL blocks tokio runtime, crash risk

**Decision**: Long-lived worker via JSON-RPC over stdin/stdout. Avoids Python startup overhead per task. Crash isolation (subprocess dies, server stays up). Auto-restart possible.

**Consequences**: Need lifecycle management (spawn, health check, crash detection). Single worker = sequential task execution (matches nats_worker pattern).

---

## Design Decision: JSON-RPC 2.0 vs custom protocol

**Context**: Need a protocol for Rust ↔ Python communication.

**Options**:
1. JSON-RPC 2.0 (chosen) — standard, has request/response/notification types
2. Custom JSON messages — simpler but no standard
3. gRPC over Unix socket — overengineered for local IPC

**Decision**: JSON-RPC 2.0 over newline-delimited stdin/stdout. Standard protocol, notification support for progress, debuggable.

---

## Three-level degradation chain

```
submit_task:
  1. NATS available?           → publish uc.task.submit
  2. local_worker available?   → JSON-RPC submit
  3. fallback                  → newline-split (no execution)
```

Health endpoint exposes which level is active:

```rust
match &self.inner.local_worker {
    Some(bridge) if bridge.is_available() => ("healthy", "connected"),
    Some(_) => ("unhealthy", "process died"),
    None => ("unavailable", "not started"),
}
```
