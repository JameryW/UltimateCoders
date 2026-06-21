# Rust→Python pause/resume 反向同步

## Goal

当 Rust gRPC server 收到 PauseTask/ResumeTask 请求时，Python 侧的 Orchestrator 能感知到状态变化，停止/恢复 subtask 调度。

## What I already know

* Rust gRPC handler `pause_task`/`resume_task` 只更新 TaskStore，**不发布 NATS 事件**
* Python Orchestrator.pause_task() 会调 `engine.pause_task()` 同步到 Rust，但反向路径不存在
* Rust NATS subscriber 已订阅 `uc.task.update` 和 `uc.task.event`，能处理来自 Python 的消息
* Python NatsWorker 只订阅 `uc.task.submit` 和 `uc.dashboard.>`，**不订阅** `uc.task.update` 或 `uc.task.event`
* Dashboard/TUI 通过 gRPC WatchTask stream 或 SSE 获取事件，pause/resume 后 TaskStore 状态变了但 Orchestrator 不知道

## Assumptions (temporary)

* Rust 侧需要在 pause/resume gRPC handler 中发布 NATS 事件（`uc.task.event` type=`task_paused`/`task_resumed`）
* Python 侧需要在 NatsWorker 中订阅 `uc.task.event`，收到 pause/resume 事件后调用 Orchestrator.pause_task()/resume_task()
* Orchestrator.pause_task()/resume_task() 已有 engine 同步逻辑，但被 NATS 触发时需要避免循环（Python→Rust→NATS→Python）

## Open Questions

* 循环检测：Python 收到 NATS pause 事件后调 Orchestrator.pause_task()，该方法会调 engine.pause_task()（gRPC），Rust 又会发 NATS 事件 → 如何打破循环？

## Requirements

1. Rust gRPC server 在 PauseTask/ResumeTask handler 成功后，发布 `uc.task.event`（type=`task_paused`/`task_resumed`）
2. Python NatsWorker 订阅 `uc.task.event`，处理 `task_paused`/`task_resumed` 事件
3. Python Orchestrator 收到事件后更新本地 task 状态，**不**再反向调 engine（避免循环）
4. 循环防护：Rust 侧 dedup（已有 message_id 机制），Python 侧标记事件来源

## Acceptance Criteria

- [ ] Rust PauseTask handler 成功后发布 `uc.task.event` type=`task_paused`
- [ ] Rust ResumeTask handler 成功后发布 `uc.task.event` type=`task_resumed`
- [ ] Python NatsWorker 订阅 `uc.task.event`
- [ ] Python 收到 `task_paused` 事件后 Orchestrator 本地状态变为 Paused（不调 engine）
- [ ] Python 收到 `task_resumed` 事件后 Orchestrator 本地状态变为 InProgress（不调 engine）
- [ ] 无循环：Python→Rust→NATS→Python 不会无限循环
- [ ] 已暂停的 task 不再分配新 subtask 给 Worker

## Definition of Done

* Tests added/updated
* Lint / typecheck / CI green
* 无循环风险

## Out of Scope

* 多 Worker 分布式（#8/#9，单独规划）
* Subtask 级别的 pause/resume
* 持久化 pause 状态到 PostgreSQL

## Decision (ADR-lite)

**Context**: 需要防止 Python→Rust→NATS→Python 循环
**Decision**: 方案 A — Orchestrator 新增 `pause_task_local()`/`resume_task_local()`，只改本地状态不调 engine。NatsWorker 收到 NATS 事件后调 `_local` 版本。
**Consequences**: 不改 NATS 协议，最简实现。如果未来需要更多事件源区分，再升级为 source 字段方案。

## Technical Notes

### 数据流

```
当前（Python→Rust 单向）:
  Python Orchestrator.pause_task() → engine.pause_task() (gRPC) → Rust TaskStore 更新
  Rust TaskStore 更新 → WatchTask stream → TUI/Dashboard 看到 paused

缺失（Rust→Python 反向）:
  TUI/Dashboard PauseTask (gRPC) → Rust TaskStore 更新 → ❌ Python 不知道
```

### 关键文件

* `crates/uc-grpc/src/server.rs` — gRPC handler + NATS subscriber + NatsTaskEvent
* `python/ultimate_coders/nats_worker.py` — NatsWorker (订阅) + NatsPublisher (发布)
* `python/ultimate_coders/agent/orchestrator.py` — Orchestrator.pause_task()/resume_task()

### 循环防护方案

Rust 已有 `check_and_record_message_id` dedup 机制。Python 侧需要：
1. Orchestrator 新增 `pause_task_local()`/`resume_task_local()` 方法（只改本地状态，不调 engine）
2. NatsWorker 收到事件后调 `_local` 版本
3. 或者：在事件 data 中加 `source` 字段，Python 检查 `source != "python"` 才处理
