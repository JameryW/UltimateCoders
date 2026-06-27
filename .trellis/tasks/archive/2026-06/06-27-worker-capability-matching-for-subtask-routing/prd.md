# Worker Capability Matching for Subtask Routing

## Goal

让 subtask 可以声明所需的 capability（如 `rust`, `python`, `docker`），dispatch 时只路由到具备这些能力的 worker，避免任务发给不具备相关工具/语言环境的 worker 执行失败。

## Requirements

* `Subtask` 增加 `required_capabilities: Vec<String>` 字段（三端同步：Rust / Python / TypeScript）
* `NatsSubtaskExecute` 增加 `required_capabilities` 字段并透传
* Worker 端 `_handle_subtask_execute` 收到 subtask 后检查自身 capabilities：
  - 匹配语义：ALL — worker 必须具备 subtask 要求的所有 capability
  - 不匹配 → NACK（NATS 自动重新投递给其他 worker）
  - 无 required_capabilities → 任何 worker 都可接受（向后兼容）
* `select_next_subtask` 按 capability 过滤（本地调度路径）

## Decision (ADR-lite)

**Context**: NATS 路由策略选择 — 如何将 subtask 路由到匹配的 worker
**Decision**: 统一 subject + Worker 端过滤
**Consequences**: 简单实现，NACK 机制自动处理重试；若大多数 worker 不匹配则多次 NACK，但目前 worker 数量少不影响

## Acceptance Criteria

* [ ] `required_capabilities` 在 Rust/Python/TypeScript 三端同步
* [ ] `NatsSubtaskExecute` 透传 `required_capabilities`
* [ ] Worker `_handle_subtask_execute` 检查 ALL 匹配，不匹配则 NACK
* [ ] 无 `required_capabilities` 的 subtask 可被任何 worker 接受
* [ ] `select_next_subtask` 按 capability 过滤
* [ ] Rust 编译通过，现有测试不受影响

## Definition of Done

* Rust 编译通过
* 三端类型同步
* 向后兼容

## Out of Scope

* Capability 优先级排序
* 动态 capability 发现
* Dashboard UI 展示 worker capabilities
* OMP UI 编辑 required_capabilities
* Subject-based 分区路由

## Technical Notes

* 关键文件：
  - `crates/uc-types/src/agent.rs` — Subtask + WorkerInfo
  - `crates/uc-grpc/src/server.rs` — NatsSubtaskExecute
  - `python/ultimate_coders/agent/types.py` — Python Subtask
  - `python/ultimate_coders/nats_worker.py` — _handle_subtask_execute
  - `python/ultimate_coders/agent/orchestrator.py` — select_next_subtask
  - `packages/uc-orchestrator/src/orchestrator/scheduler.ts` — SubtaskDef
* NATS queue group `"workers"` 订阅 `uc.subtask.execute` — 竞争消费模式
* Worker NACK 后 NATS 自动重新投递
