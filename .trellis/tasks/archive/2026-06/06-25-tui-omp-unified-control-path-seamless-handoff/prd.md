# TUI-OMP Unified Control Path — Seamless Handoff

## Goal

让 TUI (Ink) 和 Dashboard (Web) 的任务控制操作（submit/pause/resume/cancel）能真正影响 UCOrchestrator 的执行状态。同时让 OMP TUI 的操作也能被 TUI/Dashboard 实时看到。

## Requirements

1. **UCOrchestrator 订阅 NATS 控制事件** — 监听 `task_paused`/`task_resumed`/`task_cancelled`，收到后执行对应方法
2. **轮询 fallback** — NATS 不可用时，Orchestrator 定期轮询 gRPC TaskStore 检查 controlState 变化
3. **新增 CancelTask RPC** — proto 定义 + Rust server 实现 + TUI/Dashboard client 调用
4. **TUI/Dashboard 客户端更新** — TUI 和 Dashboard 调用 CancelTask RPC，pause/resume 已有

## Acceptance Criteria

* [ ] TUI 暂停任务 → gRPC PauseTask → NATS task_paused → Orchestrator 收到并暂停
* [ ] TUI 恢复任务 → gRPC ResumeTask → NATS task_resumed → Orchestrator 收到并恢复
* [ ] TUI 取消任务 → gRPC CancelTask → NATS task_cancelled → Orchestrator 收到并取消
* [ ] Dashboard 同样能执行上述操作
* [ ] NATS 不可用时，Orchestrator 轮询 gRPC TaskStore 作为 fallback
* [ ] WatchTask stream 反映状态变化
* [ ] CancelTask RPC 在 proto + Rust server 中实现
* [ ] TUI useGrpcClient 暴露 cancelTask 方法

## Definition of Done

* Rust: CancelTask RPC + proto 更新 + 编译通过 + 单元测试
* TypeScript: NATS subscriber + 轮询 fallback + 编译通过
* 集成验证: 控制信号端到端传播

## Out of Scope

* GrpcBridge HTTP+JSON → native gRPC 升级
* 状态 reconciliation (定期全量同步)
* uc-rpc-server 事件转发
* Orchestrator health endpoint

## Technical Approach

### 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 控制信号传播 | NATS subscription (方案 A) | 已有基础设施，生产级可靠 |
| NATS client 位置 | UCOrchestrator 内部 (方案 2) | 干净，不依赖 uc-rpc-server |
| NATS 不可用 fallback | 轮询 gRPC TaskStore (方案 2) | 保证本地开发体验 |
| CancelTask RPC | 新增到 proto + server (方案 1) | cancel ≠ pause，需要独立 RPC |

### 实现步骤

**Step 1: CancelTask proto + Rust server**
- proto: 加 `CancelTaskRequest` / `CancelTaskResponse` 消息 + `CancelTask` RPC
- Rust: 实现 `cancel_task()` — 更新 TaskStore，广播事件，发布 NATS `task_cancelled`
- TUI client: 加 `cancelTask()` 方法

**Step 2: UCOrchestrator NATS subscriber**
- 加 NATS client 依赖 (nats.ts / nats.ws)
- 订阅 `uc.task.control` subject（统一控制事件）
- 收到 paused/resumed/cancelled 事件 → 调用对应方法
- NATS 连接失败 → 启动轮询 fallback

**Step 3: 轮询 fallback**
- 定期 (2s) 调用 `GrpcBridge.getTask(taskId)` 检查 controlState
- 如果 gRPC TaskStore 的 controlState 与本地不一致 → 同步
- 只在 NATS 断开时激活

## Technical Notes

* 研究：research/architecture-gaps.md — 7 个 gap 详细分析
* Orchestrator pause/resume 在 wave boundary 检查 controlState
* gRPC server 已发布 NATS `task_paused`/`task_resumed` 事件
* 新增 `task_cancelled` NATS 事件
* Proto 文件：crates/uc-grpc/proto/engine.proto
