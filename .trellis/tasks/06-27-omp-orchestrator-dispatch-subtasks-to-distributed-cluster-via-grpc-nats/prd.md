# OMP Orchestrator: Dispatch Subtasks to Distributed Cluster

## Goal

OMP orchestrator 强制将所有 subtask 分发给 gRPC server 的分布式 worker 集群执行，禁止本地 runSubprocess 编码任务。没有远程 worker 时任务失败，不回退本地执行。

## What I already know

* gRPC server 已有完整的 NatsSubtaskExecute 协议
* Python NatsWorker 已实现远程 subtask 执行
* Worker 通过 heartbeat 隐式注册
* OMP upsertTask() 不触发 dispatch_ready_subtasks()——关键缺口
* OMP ControlSignalSubscriber 已有 NATS 连接 + 轮询机制

## Requirements

1. OMP 提交任务后，subtask 必须通过 gRPC server → NATS → 远程 worker 执行
2. OMP 轮询远程 subtask 状态，更新本地 TaskState
3. 远程 subtask 完成后，结果写入本地 TaskStore
4. 没有远程 worker 时，任务标记 failed（"No remote workers available"）
5. 远程 subtask 超时（默认 10min），OMP 标记 failed
6. Decompose 和 review 仍在本地执行（它们是 OMP agent 调用，不是编码任务）

## Acceptance Criteria

* [ ] /uc submit 后 subtask 通过 gRPC → NATS 分发到远程 worker
* [ ] 远程 worker 执行完成后，OMP 更新本地 task 状态
* [ ] 没有 worker 时任务立即 failed
* [ ] 超时的远程 subtask 标记 failed
* [ ] Decompose 和 review 仍本地执行

## Definition of Done

* TypeScript + Rust 编译通过
* 手动验证：启动 gRPC server + NATS worker → /uc submit → 远程执行 → 结果回传

## Out of Scope

* 混合模式（同一任务部分本地部分远程）
* OMP 直接发布 NATS 消息
* 新增 gRPC streaming RPC
* Worker 指定/亲和性调度
* 远程 subtask 实时进度流

## Technical Approach

### 核心流程

```
OMP submitTask → 本地 decompose → upsertTask(description + subtasks) → gRPC server
  → gRPC server: update_task 触发 dispatch_ready_subtasks()
  → NATS uc.subtask.execute → Python NatsWorker 执行
  → NatsWorker 结果 → NATS uc.task.update → gRPC server 更新 TaskStore
  → OMP 轮询 getTask() → 更新本地 TaskState
```

### 改动清单

1. **gRPC server**: `update_task()` 添加 `dispatch_ready_subtasks()` 调用
2. **OMP orchestrator**: `executeSubtask()` 改为远程分发 + 轮询，而非 `runSubprocess`
3. **OMP orchestrator**: 新增 `executeSubtaskRemote()` 方法
4. **OMP orchestrator**: 添加轮询逻辑获取远程 subtask 状态
5. **OMP orchestrator**: 移除/跳过 `runSubprocess` 编码路径
6. **保持**: decompose + review 仍用 `runSubprocess`

### executeSubtaskRemote() 设计

```typescript
private async executeSubtaskRemote(def, task, ctx): Promise<SubtaskResult> {
  // 1. 确保 gRPC server 有这个 task 和 subtasks（upsertTask 已在 executeWaves 中完成）
  // 2. 等待远程 worker 完成轮询
  // 3. 轮询 bridge.getTask(task.id) 检查 subtask 状态
  // 4. 超时或远程完成时返回结果
}
```

轮询参数：
- 间隔：2s（复用 ControlSignalSubscriber 的 pollIntervalMs）
- 超时：subtaskTimeoutMs（默认 600s）
- 使用 task 的 AbortController 支持取消

### worker 可用性检查

`checkWorkerAvailability()` 已实现。当 `!bridge.isConnected()` 时走降级模式（假设本地 worker 可用）的逻辑需要修改——如果没有远程 worker，应该直接 failed。

但根据之前的 P3 修复，当 gRPC 断开时我们假设本地可用。现在既然要强制远程执行，需要调整：
- 如果 `!bridge.isConnected()` → failed（"gRPC server unavailable — remote execution required"）
- 如果 `bridge.isConnected()` 但 `availableCount === 0` → failed（"No remote workers available"）

## Decision (ADR-lite)

**Context**: OMP 需要将 subtask 分发给远程集群，但现有 gRPC server 的 dispatch 仅在 NATS update 路径触发
**Decision**: Approach A — 修改 update_task 触发 dispatch + OMP 轮询结果
**Consequences**: 最小改动，复用现有基础设施，但需要 OMP 轮询结果（2s 间隔）

## Technical Notes

* `crates/uc-grpc/src/server.rs:522` — update_task 需要添加 dispatch_ready_subtasks() 调用
* `python/ultimate_coders/nats_worker.py` — NatsWorker 已监听 uc.subtask.execute
* `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — executeSubtask 改为远程
* `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` — 已有轮询机制

## Research References

* [`research/grpc-nats-subtask-execute-protocol.md`](research/grpc-nats-subtask-execute-protocol.md)
* [`research/python-nats-worker.md`](research/python-nats-worker.md)
* [`research/grpc-taskservice-distributed-path.md`](research/grpc-taskservice-distributed-path.md)
* [`research/worker-registration.md`](research/worker-registration.md)
* [`research/omp-grpc-bridge-remote-dispatch.md`](research/omp-grpc-bridge-remote-dispatch.md)
