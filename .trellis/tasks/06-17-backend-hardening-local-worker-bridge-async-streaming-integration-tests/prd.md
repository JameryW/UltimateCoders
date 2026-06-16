# Backend Hardening — Local Worker Bridge Async Streaming + Integration Tests

## Goal

让 local worker bridge 真正可用：submit_task 异步执行、broadcast 实时推送、worker 崩溃重启恢复、并发任务排队、统一事件源、集成测试覆盖。

## Requirements

### P0: submit_task 异步 + broadcast 实时推送
* `submit_task` gRPC 立即返回 task_id（status=Planning）
* Rust 端把任务描述发给 Python worker，不等结果
* 新增后台 **notification reader task**：持续读 Python worker stdout，收到 `task_update` notification → 写入 TaskStore + event_store → **broadcast** 到所有 WatchTask stream
* `WatchTask` stream 改用 `tokio::broadcast::Receiver`：收到 broadcast 立即推送，不再 500ms 轮询
* broadcast channel 容量 256，多 reader 支持（为多 worker/TUI + dashboard 并发预留）
* TUI 能通过 WatchTask 看到 subtask 状态实时变化

### P1: 统一事件源
* NATS 路径的 task update 也通过同一个 broadcast channel 推送
* `GrpcServerInner` 持有一个 `broadcast::Sender<TaskEvent>`，所有事件源（local worker / NATS）都发到这个 channel
* WatchTask stream 只需 subscribe 这个 channel

### P2: Worker 进程生命周期
* Python worker 崩溃：标记所有 in_progress 任务为 Failed，broadcast Failed 事件
* 自动重启：spawn 新 worker 进程，从 checkpoint 恢复未完成任务（利用 `CheckpointManager`）
* graceful shutdown：SIGTERM → 等 5s → SIGKILL
* 并发 submit_task 排队：worker 单线程顺序执行，Rust 端用 `mpsc` channel 排队任务请求

### P3: 集成测试
* 真实 Python 进程 + Mock LLM
* 测试场景：
  - `submit_task_via_bridge`：提交任务 → WatchTask stream 收到 Planning → InProgress → Completed 事件
  - 并发 submit：两个任务排队执行，第一个完成前第二个 status=Queued
  - Worker 崩溃：任务标记 Failed，worker 重启
  - Worker 不可用：fallback 到 local decomposition
  - WatchTask broadcast：多个 subscriber 都收到同一事件

### P4: Python 端清理
* 不访问 `_select_next_subtask`（private），改用公开 API（需要给 Orchestrator 加 `select_next_subtask` 公开方法）
* 添加 shutdown 信号处理（SIGINT/SIGTERM → 停止读取 stdin → 退出）
- 添加 `shutdown` JSON-RPC method

## Acceptance Criteria

* [ ] `submit_task` gRPC 在 <1s 内返回 task_id（status=Planning）
* [ ] WatchTask stream 能看到 subtask 从 assigned → in_progress → completed，延迟 <100ms
* [ ] NATS task update 和 local worker update 走同一个 broadcast channel
* [ ] 并发 submit_task 排队执行，不丢任务
* [ ] Worker 崩溃时 in_progress 任务标记 Failed，worker 自动重启
* [ ] 集成测试覆盖: submit_via_bridge, WatchTask broadcast, worker crash + restart, concurrent submit, fallback
* [ ] Python local_worker 不访问 Orchestrator private methods
* [ ] `cargo test` + `cargo clippy` + `cargo fmt` 全绿

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Out of Scope

* Python Orchestrator/Worker 核心逻辑变更（只加公开方法）
* TiKV/Qdrant/PostgreSQL 真实存储（继续 in-memory fallback）
* LLM 真实调用（集成测试用 mock）
* TUI 前端改动（只改后端 API 行为，TUI 已有的 WatchTask 消费逻辑不需改）
* 多 worker 并行执行（MVP 只一个 worker，但 broadcast channel 预留多 reader）

## Decision (ADR-lite)

**Context**: TUI 提交任务后，`submit_task_via_bridge` 阻塞等待完成，WatchTask 轮询 500ms 看不到中间状态。
**Decision**: 方案 B — submit_task 立即返回，后台 notification reader + `tokio::broadcast` 实时推送。
**Consequences**: 延迟从 500ms 降到 <1ms，但改动范围更大（改 WatchTask stream 实现、加 notification reader task、加 broadcast channel）。NATS 和 local worker 统一事件源，减少重复逻辑。

## Technical Approach

### Architecture

```
TUI ──WatchTask──→ broadcast::Receiver<TaskEvent>
                        ↑
               broadcast::Sender<TaskEvent>
                        ↑
        ┌───────────────┼───────────────┐
        │               │               │
  local_worker    NATS subscriber   (future: more workers)
  notifications   task updates
        ↑
  notification reader task
  (reads Python stdout, parses JSON-RPC notifications)
```

### Key changes

1. **GrpcServerInner** 新增 `event_tx: broadcast::Sender<TaskEvent>`
2. **GrpcServer::new()** 改回 sync（worker spawn 改为 lazy/background）
3. **LocalWorkerBridge** 新增 `start_notification_reader()` → tokio::spawn 后台 task
4. **submit_task** 改为：创建 task (Planning) → 发 JSON-RPC → 立即返回
5. **WatchTask** 改为 subscribe broadcast channel
6. **Task queue**: `mpsc::Sender<SubmitTaskRequest>` + 后台 task 顺序执行
7. **Worker crash**: notification reader 检测 stdout EOF → broadcast Failed → restart

### Implementation Plan

* PR1: broadcast channel + WatchTask stream 改造（不含 local worker）
* PR2: LocalWorkerBridge async 改造 + notification reader + task queue
* PR3: Worker crash/restart + 统一事件源（NATS 也走 broadcast）
* PR4: Python 端清理 + 集成测试

## Technical Notes

### Key files
* `crates/uc-grpc/src/server.rs` — GrpcServer, submit_task, WatchTask stream, TaskStore
* `crates/uc-grpc/src/local_worker.rs` — LocalWorkerBridge
* `python/ultimate_coders/local_worker.py` — Python JSON-RPC worker
* `python/ultimate_coders/agent/orchestrator.py` — Orchestrator (has _select_next_subtask)
* `crates/uc-engine/src/checkpoint.rs` — CheckpointManager (for recovery)

### Existing patterns
* `tokio::broadcast` 在 `dashboard/app.py` SSE stream 类似用途
* `CheckpointManager::recover()` 可用于 worker 重启后恢复任务状态
* `TaskStore::events` 已有 event 存储，但 local worker 路径没写入
