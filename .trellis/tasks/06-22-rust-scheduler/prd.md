# Rust调度器 — 依赖解析与subtask发布

## Goal

在 Rust `SchedulerService.dispatch()` 被触发时，将 ScheduledTask 提交为 Orchestrator 任务，利用 subtask 的 `depends_on` 做拓扑排序，按依赖顺序逐批发布到 NATS（或 LocalWorker），实现真正的调度→执行闭环。

当前状态：`ScheduleDispatcher.dispatch()` 只 log 不执行，`OrchestratorDispatcher` 有 NATS client 但只发 submit 消息不处理依赖。

## What I already know

* `SchedulerService` 有 cron/one-shot 调度 + NightWindow guard + execution history
* `ScheduleDispatcher` trait 有 `dispatch(&self, task: &ScheduledTask)` → 目前 `OrchestratorDispatcher` 只 publish `uc.task.submit`
* `decompose_task()` 做换行拆分，不处理依赖（Rust 端）；Python `Orchestrator.decompose_task()` 通过 claude -p 产出带 `depends_on` 的 subtasks
* `select_next_subtask()` 在 Python 端已有依赖感知的 subtask 选择
* `submit_task_local()` 已有 claude -p 真执行，但不感知依赖顺序
* NATS subject `uc.subtask.execute` 已存在，用于 Worker 模式分发

## Assumptions (temporary)

* 调度器触发的任务走 NATS path（有 NATS client），或 fallback 到 LocalWorker
* 依赖解析用 Kahn's algorithm（BFS topo sort），O(V+E)
* 环检测必须做，有环 → 任务失败
* 不改 proto，不改 Python Orchestrator 核心逻辑

## Requirements

### R1: 拓扑排序依赖解析

* 新增 `fn resolve_execution_order(subtasks: &[Subtask]) -> Result<Vec<Vec<TaskId>>, EngineError>`
* 返回 `Vec<Vec<TaskId>>` — 每层是可并行执行的 subtask IDs（同层无依赖关系）
* 有环 → `Err(EngineError::TaskError("circular dependency"))`
* 孤立 subtask（无依赖）在第一层

### R2: SchedulerDispatcher 真执行

* `OrchestratorDispatcher.dispatch()` 改为：
  1. submit task 到 Orchestrator（publish `uc.task.submit`）
  2. 等待 `uc.task.update` 返回分解结果（超时 60s）
  3. 对 subtasks 做 `resolve_execution_order()`
  4. 逐层发布 `uc.subtask.execute`（同层并行，层间等上一层全部完成）
* 无 NATS 时 fallback 到 `submit_task_local()`（已有 claude -p 真执行）

### R3: 事件驱动层间调度

* 发布一层 subtask 后，subscribe `uc.task.event` 等待该层所有 subtask completed/failed
* 全部完成 → 发布下一层；任一 failed 且不可恢复 → 中止剩余层，标记任务 Failed
* 超时保护：单层 600s 无完成事件 → 标记超时 failed

## Acceptance Criteria

- [ ] `resolve_execution_order()` 正确拓扑排序，同层无依赖
- [ ] 循环依赖返回 Err
- [ ] 单 subtask 无依赖 → 一层一个元素
- [ ] A→B→C 线性 → 3 层各 1 元素
- [ ] A→C, B→C 菱形 → 2 层：[A,B], [C]
- [ ] `dispatch()` 触发后，subtask 按依赖顺序执行
- [ ] 无 NATS 时 fallback 到 local 真执行
- [ ] `cargo test -p uc-engine` 通过
- [ ] `cargo check -p uc-grpc --features messaging` 通过

## Definition of Done

* Tests added for `resolve_execution_order()` (unit)
* Tests added for dispatch flow (integration with mock NATS)
* Lint / typecheck / CI green
* No proto changes

## Out of Scope

* 多 Worker 负载均衡（已有 06-22-worker 任务）
* subtask 重试（已有 Worker 失败重分配任务）
* 动态依赖添加（执行中发现新依赖）
* 优先级调度（已有 priority 字段，当前忽略）

## Technical Approach

### R1: 拓扑排序

在 `crates/uc-engine/src/scheduler/` 新增 `dependency.rs`:

```rust
pub fn resolve_execution_order(
    subtasks: &[uc_types::Subtask],
) -> Result<Vec<Vec<uc_types::TaskId>>, EngineError> {
    // Kahn's algorithm: compute in-degrees, BFS layer by layer
    // Detect cycle: if not all nodes visited → circular dependency
}
```

### R2: OrchestratorDispatcher 真执行

`crates/uc-engine/src/scheduler/dispatcher.rs`:

* `dispatch()` publish `uc.task.submit` → wait `uc.task.update` (reply subject) → parse subtasks → resolve order → publish per layer
* Fallback (no NATS): log warning, no-op (local path handled by submit_task_local)

### R3: 层间事件驱动

* Per-layer: publish `uc.subtask.execute` for all subtasks in layer
* Subscribe `uc.task.event` filter subtask_completed/failed for current layer IDs
* All completed → next layer; any unrecoverable fail → abort + TaskFailed
* Timeout: 600s per layer

## Decision (ADR-lite)

**Context**: 调度器如何将 ScheduledTask 变成实际执行？
**Decision**: OrchestratorDispatcher 发 submit → 等 update → topo sort → 逐层 publish subtask.execute
**Consequences**: 需要 NATS reply 机制等 update（或轮询 task store），增加 dispatch 耗时；但这是调度→执行闭环的最小改动路径

## Technical Notes

* 关键文件: `crates/uc-engine/src/scheduler/dispatcher.rs`, `crates/uc-engine/src/scheduler/service.rs`, `crates/uc-engine/src/scheduler/mod.rs`
* 新增文件: `crates/uc-engine/src/scheduler/dependency.rs`
* NATS subjects: `uc.task.submit`, `uc.task.update`, `uc.subtask.execute`, `uc.task.event`
* Python 依赖解析: `orchestrator._check_dependencies()`, `select_next_subtask()`
