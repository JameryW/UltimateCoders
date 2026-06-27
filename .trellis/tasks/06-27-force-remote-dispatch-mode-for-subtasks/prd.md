# Force-Remote Dispatch Mode for Subtasks

## Goal

为 subtask 添加 `dispatch_mode` 字段，使编排方可以强制指定 subtask 必须由远程 worker 执行（而非本地 fallback），在 NATS 不可用时返回错误而非静默降级。

## What I already know

* 当前 `publish_ready_subtasks` / `dispatch_ready_subtasks` 在 NATS publish 失败时仅 revert status 到 Pending，无强制远程语义
* `NatsSubtaskExecute` 没有 dispatch_mode 字段
* Python `Subtask` dataclass 没有 dispatch_mode 字段
* OMP `SubtaskDef` interface 没有 dispatch_mode 字段
* `GrpcServer` 有 NATS client 可选，NATS 不可用时无 fallback 路径（publish_ready_subtasks 的 no-nats 版本是空函数）
* Python `Engine` 有 gRPC→local fallback 机制，但这是 Engine 层面的，不是 subtask 路由层面

## Assumptions (temporary)

* `dispatch_mode` 三值枚举：`Local` / `Remote` / `PreferRemote`（默认 PreferRemote = 当前行为）
* `Remote` 模式下 NATS 不可用 → revert to Pending + retry_count 递增，超限（3次）后标记 Failed
* `PreferRemote` 模式下 NATS 不可用 → revert to Pending（当前行为）
* `Local` 模式 → 跳过 NATS，直接本地执行（为未来预留，本次不实现本地执行路径）

## Open Questions

* ~~Remote 失败时的行为？~~ → revert to Pending + retry_count 递增，超限 3 次后标记 Failed

## Requirements

* Rust `uc-types` 的 `Subtask` 添加 `dispatch_mode: DispatchMode` 枚举（默认 PreferRemote）
* Rust `uc-grpc` 的 `NatsSubtaskExecute` 添加 `dispatch_mode` 字段并透传
* `publish_ready_subtasks` / `dispatch_ready_subtasks` 检查 dispatch_mode：
  - `Remote` + NATS publish 失败 → revert to Pending, retry_count 递增；retry_count ≥ 3 时标记 Failed
  - `PreferRemote` + NATS publish 失败 → revert to Pending（当前行为）
  - `Local` → 跳过 NATS publish（本次 no-op，日志记录）
* Python `Subtask` dataclass 添加 `dispatch_mode` 字段
* OMP `SubtaskDef` 添加 `dispatchMode` 字段
* gRPC proto `SubtaskProto` 添加 `dispatch_mode` 字段

## Acceptance Criteria (evolving)

* [ ] `DispatchMode` 枚举在 uc-types 中定义（Local / Remote / PreferRemote），Default = PreferRemote
* [ ] `Subtask.dispatch_mode` 在 Rust / Python / TypeScript 三端同步
* [ ] `NatsSubtaskExecute.dispatch_mode` 透传
* [ ] `publish_ready_subtasks` 对 Remote 模式 NATS 失败：revert to Pending + retry_count 递增，≥3 次 Failed
* [ ] `dispatch_ready_subtasks` 同上
* [ ] PreferRemote 模式保持当前行为（向后兼容）
* [ ] `cargo check` 通过
* [ ] 现有测试不受影响

## Definition of Done

* Rust 编译通过 (`cargo check`)
* 现有测试通过
* 三端类型同步（Rust / Python / TypeScript）
* 向后兼容（默认 PreferRemote = 当前行为）

## Out of Scope

* `Local` 模式的实际本地执行路径（仅预留枚举值）
* Worker capability 匹配 / worker 选择逻辑
* OMP UI 展示 dispatch_mode
* Dashboard 展示 dispatch_mode
* gRPC proto 重新生成（仅手动添加字段）

## Technical Notes

* 关键文件：
  - `crates/uc-types/src/agent.rs` — Subtask 定义
  - `crates/uc-grpc/src/server.rs` — NatsSubtaskExecute, publish_ready_subtasks, dispatch_ready_subtasks
  - `python/ultimate_coders/agent/types.py` — Python Subtask
  - `packages/uc-orchestrator/src/orchestrator/scheduler.ts` — SubtaskDef
  - `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — SubtaskResult
  - proto 文件：`crates/uc-grpc/proto/engine.proto`
* publish_ready_subtasks 有两个版本（messaging / no-messaging feature gate），都需要处理
* dispatch_ready_subtasks 是独立函数，被 heartbeat monitor 调用
