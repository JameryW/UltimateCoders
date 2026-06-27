# Research: gRPC Server NatsSubtaskExecute Protocol

- **Query**: How does the gRPC server dispatch subtasks to NATS workers? What messages does it send? How does it track results?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `crates/uc-grpc/src/server.rs` | Main gRPC server + TaskStore + NATS subscriber + subtask dispatch |
| `crates/uc-grpc/src/dashboard_service.rs` | DashboardService NATS passthrough (ListWorkers fallback from heartbeat data) |
| `crates/uc-grpc/proto/engine.proto` | Proto definitions for TaskService, DashboardService, EngineService |
| `.trellis/spec/backend/nats-bridge-spec.md` | NATS bridge spec (subject constants, payload formats, status mappings) |

### NATS Subjects Used for Subtask Dispatch

| Subject | Direction | Purpose |
|---------|-----------|---------|
| `uc.task.submit` | gRPC/Dashboard -> Python | New task submission (triggers decomposition) |
| `uc.task.update` | Python -> gRPC | Task/subtask status update (after worker execution) |
| `uc.task.event` | Bidirectional | Real-time events (tool calls, pause/resume, completion/failure) |
| `uc.heartbeat` | Python -> gRPC | Consumer/worker heartbeat |
| `uc.subtask.execute` | Rust -> Worker queue group | Subtask execution dispatch |
| `uc.dashboard.>` | Dashboard -> Python | NATS request-reply for DashboardService passthrough |
| `uc.dashboard.snapshot` | Python -> gRPC | Periodic dashboard snapshot (5s interval) |

Defined in `crates/uc-grpc/src/server.rs` lines 34-46 and mirrored in `python/ultimate_coders/nats_worker.py` lines 50-54.

### NatsSubtaskExecute Payload

Defined at `crates/uc-grpc/src/server.rs` lines 122-139:

```rust
pub struct NatsSubtaskExecute {
    pub message_id: Option<String>,    // dedup key
    pub task_id: String,
    pub subtask_id: String,
    pub description: String,
    pub expected_output: String,       // always empty currently
    pub file_constraints: Vec<String>, // always empty currently
    pub timeout_seconds: u64,          // default 600
    pub retry_count: u32,              // incremented on re-dispatch after failure
}
```

### Dispatch Flow

1. **Task Submission**: `submit_task` RPC (line 2251) creates a `Planning` placeholder in TaskStore, publishes `NatsTaskSubmit` to `uc.task.submit`

2. **Python Decomposition**: Python NatsWorker receives submit, calls `Orchestrator.submit_task()` which splits by newlines (simple decomposition), publishes `NatsTaskUpdate` to `uc.task.update` with subtasks

3. **NATS Subscriber Processing**: `spawn_nats_subscriber` (line 1445) receives `uc.task.update`, applies it to TaskStore via `apply_update()`, then calls `dispatch_ready_subtasks()` (line 1594)

4. **Dispatch Ready Subtasks**: `dispatch_ready_subtasks()` (line 1708) or `GrpcServer::publish_ready_subtasks()` (line 1362):
   - Acquires TaskStore lock, calls `get_ready_subtasks(task_id)`
   - A subtask is "ready" when: status is `Pending`, all `depends_on` subtasks are `Completed`, and task status is `InProgress`
   - Marks ready subtasks as `Assigned` (to prevent re-dispatch)
   - Publishes `NatsSubtaskExecute` for each ready subtask to `uc.subtask.execute`
   - On publish failure, reverts subtask back to `Pending`

5. **Result Tracking**: Workers publish results via `uc.task.update` (with completed/failed subtask). The NATS subscriber applies these updates, which may unblock dependent subtasks, triggering another `dispatch_ready_subtasks()` call.

### Automatic Re-dispatch After Dependency Completion

After each `uc.task.update` is processed (line 1594), `dispatch_ready_subtasks` is called. This means when a subtask completes and its dependents become ready, those dependents are automatically dispatched to NATS.

### Worker Failure Detection and Reassignment

- `spawn_heartbeat_monitor` (line 1661) runs every 30 seconds
- Checks for stale workers via `mark_stale_workers(heartbeat_timeout)` (line 1683)
- Stale workers are removed from `worker_heartbeats` map
- `reassign_stale_subtasks()` (line 889) resets subtasks from `InProgress/Assigned` back to `Pending` for stale workers
- Affected tasks are then re-dispatched via `dispatch_ready_subtasks()`

### Code Patterns

- The dispatch flow is **reactive**: it runs after every `uc.task.update` message, not on a timer
- Subtask status progression: `Pending` -> `Assigned` (on dispatch) -> (worker picks up) -> `InProgress` -> `Completed/Failed`
- The `get_ready_subtasks()` method (line 927) enforces DAG dependency ordering
- NATS queue group `workers` ensures exactly one worker picks up each `uc.subtask.execute` message (handled by Python worker subscribing with `queue="workers"`)

## Caveats / Not Found

- The `NatsSubtaskExecute` payload currently always sends `expected_output: ""` and `file_constraints: []` -- these fields from the Subtask type are not populated during dispatch
- The spec at `.trellis/spec/backend/nats-bridge-spec.md` does not include `uc.subtask.execute` in its subject table -- it appears to predate the distributed worker feature
- Known limitation from spec: "Task ID mismatch: Orchestrator creates its own task_id, ignoring the one from NATS submit" -- however, the current `submit_task` code at line 739 passes `task_id=task_id or None` to the Python orchestrator, and the Python `Orchestrator.submit_task()` at line 137 accepts `task_id` parameter
