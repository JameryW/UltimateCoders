# 事件管道优化：Subtask 输出回流 + Failover 再执行 + Dashboard 无 NATS fallback

## Goal

修复事件管道中三个实际阻塞问题，使任务执行结果能完整回流到 UI，failover 能真正恢复执行，Dashboard 在无 NATS 时也能展示关键面板。

## What I already know

* Subtask 执行结果（modified_files, summary）通过 NATS `uc.task.update` 传回，但 Rust `store.apply_update()` 没有把 result 写回 subtask
* `mark_stale_workers` + `reassign_stale_subtasks` 已实现，但重分配为 Pending 的 subtask 不会被再次 dispatch
* `WatchDashboard` 依赖 Python 端 NATS snapshot，无 NATS 时 workers/scheduler/circuit_breaker 面板全空
* Rust 端 `GrpcServer` 已有 `list_workers`/`get_scheduler_status`/`get_circuit_breaker_status` 方法，可以直接构造快照
* `dispatch_ready_subtasks()` 已存在，发布 `uc.subtask.execute` 到 NATS

## Assumptions (temporary)

* 优化限于 Rust gRPC server 端 + Dashboard hook 层，不改 Python Orchestrator/Worker
* Failover 再执行走已有的 `dispatch_ready_subtasks` 路径

## Open Questions

(none — all resolved)

## Requirements (evolving)

* `spawn_heartbeat_monitor` 在 reassign 后调用 `dispatch_ready_subtasks` 触发再执行
* `WatchDashboard` 在无 NATS 时，server 端定期从 TaskStore 构造 DashboardSnapshot 并推送
* Dashboard `SubtaskSummary` 加 `result` 字段，TaskDetail 显示 subtask 执行摘要

## Acceptance Criteria (evolving)

* [ ] Worker failover 后，被重分配的 subtask 在 heartbeat monitor 周期内被再次 dispatch
* [ ] 无 NATS 时，`WatchDashboard` 仍能推送 workers/scheduler/CB 快照
* [ ] Dashboard TaskDetail 显示 subtask 的 result/summary

## Definition of Done (team quality bar)

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Technical Approach

1. **Failover 再执行**: `spawn_heartbeat_monitor` 在 `reassign_stale_subtasks` 后，对每个 affected task 调用 `dispatch_ready_subtasks`。需要把 `nats_client` 传入 monitor（已有 `_nats_client` 参数）。

2. **WatchDashboard no-NATS fallback**: 在 `dashboard_service.rs` 的 `watch_dashboard` 中，`#[cfg(not(feature = "messaging"))]` 分支改为从 TaskStore + Engine 构造 DashboardSnapshot 并每 5s 推送。需要 `task_store` + `engine` 引用。

3. **Subtask result 显示**: `SubtaskSummary` TS 类型加 `result?: string`，proto `Subtask` 已有 `result` 字段（需确认），TaskDetail 在 subtask 行显示 result summary。

## Decision (ADR-lite)

**Context**: WatchDashboard 无 NATS 时完全不可用，failover 重分配后不触发再执行，subtask 结果不显示
**Decision**: Server 端构造快照作为 fallback；heartbeat monitor 调用 dispatch；前端加 result 字段
**Consequences**: 无 NATS 时 Dashboard 功能完整但刷新率低（5s）；failover 自动恢复执行

## Out of Scope (explicit)

* Python Orchestrator/Worker 代码改动
* 多 Worker 实际分布式部署（架构已就绪，运行时配置问题）
* TUI 端改动
* 任务结果持久化到 PostgreSQL

## Technical Notes

* `apply_update()` 在 `crates/uc-grpc/src/server.rs` ~行 482
* `Subtask` 类型定义在 `crates/uc-types/src/agent.rs`，有 `result: Option<String>` 字段
* `NatsSubtaskUpdate` 在 `server.rs` ~行 73，有 `result: Option<String>`
* `dispatch_ready_subtasks()` 在 `server.rs`，已发布 `uc.subtask.execute`
* `WatchDashboard` 在 `crates/uc-grpc/src/dashboard_service.rs`
* `GrpcServer` 有 `list_workers()`/`get_scheduler_status()`/`get_circuit_breaker_status()` 可直接调用
