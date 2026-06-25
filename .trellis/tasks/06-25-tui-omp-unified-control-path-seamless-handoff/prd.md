# TUI-OMP Unified Control Path — Seamless Handoff

## Goal

让 TUI (Ink) 和 Dashboard (Web) 的任务控制操作（submit/pause/resume/cancel）能真正影响 UCOrchestrator 的执行状态，而不是只改 gRPC TaskStore 的表面数据。同时让 OMP TUI 的操作也能被 TUI/Dashboard 实时看到。

## What I already know

* 研究发现 7 个架构 gap（见 research/architecture-gaps.md）
* 最关键的 gap：Pause/Resume 从 gRPC 不传到 Orchestrator — TUI/Dashboard 暂停任务，Orchestrator 继续跑
* Orchestrator 是执行态的事实 source-of-truth，gRPC TaskStore 是展示态的 source-of-truth
* 没有 CancelTask RPC — TUI 的 cancel 只是本地状态
* uc-rpc-server 不转发事件 — Python OmpBridge 看不到进度
* GrpcBridge 用 HTTP+JSON 而非标准 gRPC

## Assumptions (temporary)

* MVP 只解决 Gap 2 (pause/resume 传播) 和 Gap 1 (cancel RPC)
* 其他 gap (事件转发、状态 reconciliation) 可以后续迭代
* 不改变 Orchestrator 本身的执行逻辑 — 只增加"接收外部控制信号"的通道

## Open Questions

* 控制信号传播机制：NATS subscription vs JSONL stdio vs HTTP callback?
* Orchestrator 进程模型：单实例长驻 vs per-request spawn?
* 是否需要同时解决事件转发（Gap 4）?

## Requirements (evolving)

* gRPC PauseTask/ResumeTask RPCs 能真正暂停/恢复 Orchestrator 执行
* 新增 CancelTask RPC，TUI/Dashboard 可以真正取消任务
* 操作后 TUI/Dashboard 能通过 WatchTask 看到状态变化

## Acceptance Criteria (evolving)

* [ ] TUI 暂停任务 → Orchestrator 停止执行下个 wave
* [ ] TUI 恢复任务 → Orchestrator 重新开始执行
* [ ] TUI 取消任务 → Orchestrator abort + cascade cancel
* [ ] Dashboard 也能执行上述操作
* [ ] 操作后 WatchTask stream 反映状态变化

## Definition of Done

* 单元测试：控制信号传播逻辑
* 集成测试：TUI → gRPC → Orchestrator round-trip
* TypeScript + Rust 编译通过

## Out of Scope (explicit)

* GrpcBridge 协议升级 (HTTP+JSON → native gRPC) — 现在能工作
* 状态 reconciliation (定期同步) — 太复杂，先不做
* uc-rpc-server 事件转发 — 可以单独迭代
* Orchestrator health endpoint for TUI — 低优先级

## Technical Notes

* 研究：research/architecture-gaps.md — 7 个 gap 详细分析
* Orchestrator pause/resume 只在 wave boundary 检查 controlState
* NATS `task_paused`/`task_resumed` 事件已由 gRPC server 发布
* Orchestrator 目前不订阅 NATS
