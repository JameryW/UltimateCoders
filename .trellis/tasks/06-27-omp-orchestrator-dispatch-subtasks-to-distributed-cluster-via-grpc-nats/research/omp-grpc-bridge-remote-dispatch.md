# Research: OMP gRPC Bridge for Remote Dispatch

- **Query**: What does the GrpcBridge already expose that could be used for remote dispatch? Does it have any subtask-level execution RPC? Can the OMP orchestrator trigger remote execution via existing gRPC methods?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | GrpcBridge class (645 lines) |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | UCOrchestrator (task lifecycle, wave execution) |
| `packages/uc-orchestrator/src/orchestrator/worker-bridge.ts` | Worker tools (uc_worker LLM tool) |
| `packages/uc-orchestrator/src/orchestrator/task-bridge.ts` | Task tools (uc_task LLM tool) |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | NATS control signal subscriber |
| `crates/uc-grpc/proto/engine.proto` | Proto definitions for all RPCs |

### GrpcBridge Methods Summary

The GrpcBridge exposes these gRPC RPCs:

| Method | Proto Service | Remote Dispatch Relevance |
|--------|--------------|--------------------------|
| `health()` | EngineService | Low -- just health check |
| `submitTask(description, projectId)` | TaskService | **HIGH** -- submits task, returns task with subtasks |
| `getTask(taskId)` | TaskService | Medium -- can poll task state |
| `listTasks()` | TaskService | Low -- listing only |
| `upsertTask(task)` | TaskService | **HIGH** -- create-or-update with subtasks |
| `pauseTask(taskId)` | TaskService | Medium -- control signal |
| `resumeTask(taskId)` | TaskService | Medium -- control signal |
| `cancelTask(taskId)` | TaskService | Medium -- control signal |
| `listWorkers()` | DashboardService | **HIGH** -- checks worker availability |
| `readMemory(...)` | EngineService | Low -- memory access |
| `writeMemory(...)` | EngineService | Low -- memory access |
| `searchMemory(...)` | EngineService | Low -- memory search |
| `searchCode(...)` | EngineService | Low -- code search |
| `indexRepo(...)` | EngineService | Low -- indexing |
| `listRepos()` | EngineService | Low -- repo listing |
| `listDir(...)` | EngineService | Low -- file browsing |
| `getFile(...)` | EngineService | Low -- file access |

### No Subtask-Level Execution RPC

The GrpcBridge does NOT expose any RPC that directly triggers subtask execution on a remote worker. There is:

- No `ExecuteSubtask` RPC in the proto
- No `DispatchSubtask` RPC
- No `AssignWorker` RPC
- No method in GrpcBridge that publishes to `uc.subtask.execute`

The existing subtask dispatch happens **entirely within the Rust gRPC server** when it processes `uc.task.update` messages from Python and calls `dispatch_ready_subtasks()`. The OMP side has no visibility into or control over this dispatch.

### How OMP Currently Triggers Remote Execution

The OMP orchestrator triggers remote execution indirectly:

1. **submitTask()**: Submits a task description to gRPC. If NATS is available, the gRPC server publishes to `uc.task.submit`, Python decomposes and the Rust server dispatches subtasks. OMP has no further involvement in the subtask dispatch.

2. **upsertTask()**: Can push task state (including subtasks) to the gRPC server's TaskStore. This is used for syncing OMP's local task state to gRPC. The server's `update_task` RPC does NOT trigger `dispatch_ready_subtasks()` -- that only happens from the NATS subscriber path.

3. **Worker availability check**: `listWorkers()` (via `bridge.listWorkers()`) is used before each wave execution to check if any workers are available.

### The Gap: OMP Cannot Dispatch Individual Subtasks Remotely

Currently, the OMP orchestrator has no way to:
- Send a specific subtask to a specific remote worker
- Dispatch a subtask to the NATS `uc.subtask.execute` subject
- Tell the gRPC server "dispatch these subtasks now"
- Track which worker is executing which subtask in the distributed cluster

The only remote dispatch path is:
```
OMP -> submitTask() -> gRPC -> NATS uc.task.submit -> Python -> NATS uc.task.update -> gRPC -> dispatch_ready_subtasks() -> NATS uc.subtask.execute -> Worker
```

This is a task-level flow. The OMP orchestrator cannot participate in the subtask-level dispatch decisions.

### What Would Be Needed for OMP to Dispatch Subtasks

To enable the OMP orchestrator to dispatch subtasks to the distributed cluster, one or more of these approaches would work:

1. **Add a gRPC RPC** like `DispatchSubtask(DispatchSubtaskRequest) returns (DispatchSubtaskResponse)` that the Rust server handles by publishing to `uc.subtask.execute`. The server would need to mark the subtask as `Assigned` in TaskStore first.

2. **Add a gRPC RPC** like `UpdateAndDispatch(UpdateTaskRequest) returns (UpdateTaskResponse)` that combines `update_task` with automatic dispatch of ready subtasks. Currently `update_task` does not call `dispatch_ready_subtasks()`.

3. **Have OMP publish directly to NATS** `uc.subtask.execute`. The OMP orchestrator already has a NATS connection via `ControlSignalSubscriber`. It could publish `NatsSubtaskExecute` messages directly, bypassing the gRPC server.

4. **Have OMP use the existing `submitTask` flow** but at the subtask level. This would require a new "subtask submission" concept where the gRPC server treats each subtask as its own task.

### Existing OMP Orchestrator Execution Model

The OMP orchestrator (`orchestrator.ts`) executes subtasks itself:
1. Decomposes task into subtasks via LLM agent
2. Builds DAG and splits into waves
3. Executes each wave via `runSubprocess` (Claude Code CLI)
4. Checks worker availability before each wave
5. All execution is local to the OMP process

There is no code path in `orchestrator.ts` that dispatches subtasks to remote workers. The `executeWave` method (line 514) uses `executeSubtaskWithRetry` which calls `runSubprocess` locally.

### ControlSignalSubscriber

The `ControlSignalSubscriber` (control-signal-subscriber.ts) subscribes to `uc.task.event` via NATS to receive pause/resume/cancel signals. It does NOT listen for subtask completion/failure events from remote workers, and it does NOT publish any messages to NATS.

## Caveats / Not Found

- The `WatchTask` gRPC streaming RPC is available but not used by the OMP orchestrator. It could potentially be used to observe remote subtask execution progress.
- The OMP's `syncTaskToGrpc` method pushes state FROM OMP TO gRPC (one-way sync). There is no reverse sync (gRPC state -> OMP state) except through the NATS control signal subscriber for pause/resume/cancel events.
- The `GrpcBridge.upsertTask()` method currently does NOT trigger `dispatch_ready_subtasks()` on the server side. This is a key gap: even if OMP creates subtasks in the gRPC TaskStore, they will never be dispatched to remote workers.
