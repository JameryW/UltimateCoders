# Research: gRPC TaskService Submit/Update Distributed Path

- **Query**: How does submit_task / update_task work for the distributed path? What happens when a task is submitted via gRPC (not OMP)? How does the server decompose and dispatch?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `crates/uc-grpc/src/server.rs` | TaskService implementation + TaskStore + NATS subscriber |
| `crates/uc-grpc/proto/engine.proto` | Proto definitions for SubmitTask/UpdateTask RPCs |
| `.trellis/spec/backend/taskservice-grpc-spec.md` | TaskService gRPC spec |

### Submit Task Flow (Distributed Path)

When NATS is available (`messaging` feature + `UC_NATS_URL` set), `submit_task` (line 2251) follows this flow:

1. **Validate**: Check `description` is non-empty
2. **Create Planning placeholder**: `TaskStore::submit_task_pending()` creates a task with:
   - Status: `Planning`
   - No subtasks (empty vec)
   - Records `TaskCreated` event
3. **Publish to NATS**: Serialize `NatsTaskSubmit { task_id, description, project_id }` and publish to `uc.task.submit`
4. **Return immediately**: Response includes `success: true`, `task_id`, `status: "Planning"`, `subtask_count: 0`
5. **Python decomposes**: Python NatsWorker picks up `uc.task.submit`, calls `Orchestrator.submit_task()`, which does newline-split decomposition
6. **Python reports back**: Publishes `NatsTaskUpdate` to `uc.task.update` with decomposed subtasks
7. **gRPC applies update**: NATS subscriber calls `TaskStore::apply_update()`, which:
   - Updates task status from `Planning` to `InProgress`
   - Upserts all subtasks from the update
8. **Auto-dispatch**: After applying update, `dispatch_ready_subtasks()` is called, which publishes `NatsSubtaskExecute` for all ready subtasks

**If NATS publish fails**: The Planning placeholder is removed from TaskStore, and the response returns `success: false` with error message "No NATS connection available. Connect NATS and start a Python worker to enable task submission." (line 2375). There is NO local fallback decomposition anymore -- Rust-side decomposition was removed.

### Update Task Flow

`update_task` RPC (line 2653):
1. Convert proto subtasks to Rust `Subtask` type
2. Call `TaskStore::update_task()` which:
   - **Create-if-not-exists**: If task_id not found AND description is non-empty, creates a new task with the given fields (preserving the client's task_id)
   - Updates task status
   - Performs **full upsert** on subtasks: matches by ID, updates status/result/description/depends_on/assigned_worker, adds new subtasks
   - Records state transition events (SubtaskStarted, SubtaskCompleted, SubtaskFailed)
   - Records SubtaskAssigned for newly added subtasks
3. Broadcasts all events to WatchTask streams
4. Returns `UpdateTaskResponse { success, task_id, status }`

### TaskStore::apply_update (NATS-driven)

`apply_update()` (line 700) handles updates from Python via NATS `uc.task.update`:
- If task_id is unknown, logs warning and does nothing (graceful)
- Updates task status with string parsing (CamelCase)
- Full upsert on subtasks:
  - **Existing subtask**: Updates status, assigned_worker, description, depends_on, result
  - **New subtask**: Creates with all provided fields from the update payload
- Subtask result is derived: success=true unless status is "failed"

### No Rust-Side Decomposition

The comment at line 2933 confirms: "decompose_task tests removed -- Rust-side decomposition no longer exists; all decomposition goes through Python Orchestrator via NATS/bridge."

When NATS is unavailable and `submit_task` is called, it returns `success: false` with the error "No NATS connection available." There is no fallback decomposition in Rust.

### Dependency-Based Dispatch

`get_ready_subtasks()` (line 927):
- Only dispatches when task status is `InProgress`
- A subtask is ready when: status is `Pending` AND all `depends_on` subtasks have status `Completed`
- Dispatched subtasks are marked `Assigned` before NATS publish

### Event Recording and Streaming

Every state change records events to both:
1. Inline `events` vec (for immediate reads and tests)
2. `EventStore` (persistent, async append)

Events are broadcast via `broadcast::Sender<TaskEvent>` channel (capacity 256) to all `WatchTask` stream subscribers.

## Caveats / Not Found

- The `UpdateTaskRequest` proto does NOT have a `result` field on subtask -- only `SubtaskProto.result` exists as `optional string` (line 327 of engine.proto). The Rust `Subtask` type's `result` field is set to `None` during conversion (line 2679).
- The `apply_update` path (NATS-driven) can create new subtasks that didn't exist before, but cannot trigger dispatch independently -- it relies on `dispatch_ready_subtasks()` being called after `apply_update()`.
- The "create-if-not-exists" behavior in `update_task` enables the Python Orchestrator to re-create tasks after a server restart using a single `updateTask` call (no `submitTask` needed), as documented in the code comment at line 525.
