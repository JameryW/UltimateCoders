# Orchestrator 实时推送 + gRPC UpdateTask

## Goal

让 OMP Orchestrator 的状态变更实时推送到 Dashboard，并在 Rust gRPC server 端增加 UpdateTask RPC 使 upsertTask 真正生效。

## What I already know

* Dashboard SSE 已存在 (`/dashboard/api/stream`)，消费 NATS `uc.task.event`
* gRPC WatchTask 已存在，500ms 轮询 EventStore
* OMP Orchestrator 只做 `syncTaskToGrpc`（fire-and-forget HTTP POST），不发 NATS 事件
* gRPC TaskStore 是内存 HashMap，没有 UpdateTask RPC
* Python Orchestrator (NatsWorker) 通过 NATS 发布事件，OMP 的不行

## Requirements

### R1: Orchestrator 事件推送（通过 gRPC TaskStore 自动触发）

* OMP Orchestrator 状态变更时调 `syncTaskToGrpc`（已有）
* Rust TaskStore 在收到 SubmitTask/UpdateTask 时自动往 EventStore 写事件
* gRPC WatchTask stream 自动推送到 Dashboard hooks
* **不需要 OMP 侧改推送逻辑**，只需让 Rust TaskStore 在 mutate 时 emit event
* 事件类型：task_created, task_updated, subtask_started, subtask_completed, subtask_failed

### R2: gRPC UpdateTask RPC

* 在 Rust proto 增加 `UpdateTask` RPC
* 在 Rust TaskStore 实现 `update_task` 方法
* 在 GrpcBridge 实现 `updateTask` 客户端方法
* 修改 `upsertTask`：先 `getTask`，存在则调 `updateTask`，不存在则 `submitTask`

## Acceptance Criteria

* [ ] Orchestrator 状态变更后 Dashboard SSE 在 2s 内收到事件
* [ ] Dashboard 能看到 subtask_started/completed/failed 实时状态
* [ ] gRPC UpdateTask RPC 可用，proto 已更新
* [ ] Rust TaskStore.update_task 正确更新已有 task
* [ ] GrpcBridge.upsertTask 正确使用 UpdateTask 更新
* [ ] tsc --noEmit 通过

## Out of Scope

* WebSocket 替代 SSE（SSE 已够用）
* OMP 直接连接 NATS（依赖 gRPC bridge 间接推送）
* Dashboard 前端 UI 修改

## Technical Approach

### R1: 事件推送

方案 B（已选）：通过 gRPC TaskStore 自动触发。
- Rust TaskStore 在 SubmitTask/UpdateTask 时往 events Vec 推入 AgentEventType
- WatchTask stream 的 500ms 轮询自动把新事件推出去
- OMP 侧无需改推送逻辑，只需确保 syncTaskToGrpc 正确调用
- Dashboard hooks (useDashboardGrpc) 已经消费 WatchTask stream

### R2: UpdateTask RPC

Proto 变更：
```protobuf
rpc UpdateTask(UpdateTaskRequest) returns (UpdateTaskResponse);
```

Rust 实现：
- TaskStore.update_task(task_id, status, subtasks)
- 验证 task 存在，更新 status + subtasks

TypeScript client：
- GrpcBridge.updateTask(taskId, status, subtasks)
- resolveService 新增 "UpdateTask" → "TaskService"
