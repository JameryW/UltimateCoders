# Worker exec observability: progress + coding agent + gateway conn

## Goal

让用户在 dashboard web UI 和 OMP TUI 两处都能看到 worker 实时执行情况：subtask 阶段进度（phase+percent）、当前跑的 coding agent（claude-code/codex）、workflow step 链进度、gateway 连接状态。当前后端事件管道已齐全，缺口在前端渲染 + TUI 暴露。

## What I already know

### 后端事件管道（已齐全，无需改）
- worker.py 发 `subtask_started`（description + worker_id）
- worker.py 发 `subtask_progress`（phase + percent + worker_id）
  - 单 agent: preparing(10) → executing(50) → validating(80) → finalizing(95)
  - 多 agent workflow: 每 step 发 progress，phase="step 1/3: claude-code"，带 step_agent/step_status/step_summary/step_index/step_total
- worker.py 发 `subtask_completed`/`subtask_failed`
- 事件经 NATS uc.task.event → dashboard SSE /stream + REST /events
- subtask 有 agent_name 字段（types.py:101），workflow step 有 step.agent

### 前端缺口（dashboard）
- `SubtaskSummary` 类型无 phase/percent/step_agent 字段 — 不存实时进度
- `DashboardEvent.details` 是 `Record<string, unknown>` 弱类型 — phase/percent/step_agent 在里面但没结构化消费
- `TaskDetail.SubtaskProgressBar` 只按 completed/total 算总进度，不显示单 subtask 实时 phase/percent
- `WorkersPanel` 显示 load，不显示 worker 当前跑的 subtask 用哪个 agent + 实时 phase
- EventLogPanel 列事件但 progress 事件未突出展示 step_agent

### TUI 缺口（OMP uc-orchestrator）— 深层，事件管道断裂
- **事件管道断裂**：worker 发 subtask_progress → NATS uc.task.event → gRPC server 消费 → dashboard SSE。但 **OMP TUI 不在消费链**：GrpcBridge 无事件订阅/watchTask，只发 RPC。gRPC server 没把事件推给 OMP TUI。
- `OrchestratorEvents` 接口（events.ts:14）无 `subtask_progress` 类型 — 只有 start/end/failed/reviewing
- extension.ts:75 progressEvents 列表没订阅 subtask_progress
- orchestrator 不 emit subtask_progress
- progress-widget.ts:85 只按 completed/total 算 wave 进度，running subtask 只 status icon，无 phase/percent/agent
- subtask-tree-overlay.ts DAG 只 status icon，无 phase/percent/agent
- status-renderer.ts 只写 footer 状态栏
- connection_state 事件类型存在（events.ts:52），extension.ts 列表里有，但 switch 无 handler 分支

### 结论：两层工作量差异大
- **dashboard**：后端事件齐全，纯前端渲染（SubtaskSummary 加 phase/percent/step_agent 字段 + TaskDetail/WorkersPanel/EventLogPanel 消费）— 中等
- **TUI**：需补事件管道（gRPC server → OMP TUI 事件流 + orchestrator 加 subtask_progress 类型/emit + extension 订阅 + widget 渲染）— 大

### Gateway 连接状态
- GrpcBridge 有 withReconnect（连接错误重试1次）+ checkRestartMarker（读 /tmp/uc-grpc-restart-marker）
- reconnecting 互斥锁
- 缺口：连接状态无 UI 可见（连没连/断没断看不见）
- 缺口：standalone 容器 gateway 无 health_monitor（本地 run-omp.sh 有，容器无）
- 缺口：withReconnect 只重试1次，长断无指数退避

## Research References

* [`research/grpc-to-tui-event-pipe.md`](research/grpc-to-tui-event-pipe.md) — Rust gRPC server 已有多订阅 broadcast channel(cap 256)→WatchTask RPC，dashboard 经 gRPC-Web 消费；TUI GrpcBridge 同库但只发 unary。**关键发现：subtask_progress 被 Rust nats_event_to_agent_event (server.rs:2597) 静默丢弃，dashboard 现在也看不到 progress**。

## 修正：事件链路真实状态

原假设"dashboard 后端事件齐全"错误。真实链路：
worker → NATS uc.task.event → Rust gRPC server `nats_event_to_agent_event`(server.rs:2424) → broadcast channel → WatchTask RPC → dashboard SSE / TUI

`subtask_progress` 在 Rust 这层被 catch-all `_ => None` 丢弃（server.rs:2597），**未进 broadcast**。所以 dashboard + TUI **都看不到 progress**。Rust 修这层是前置依赖。

## Decision (ADR-lite)

**Context**: 用户要 dashboard + TUI 两处看 worker 实时进度（phase/percent/agent）+ gateway 连接状态 + 恢复增强。研究发现 progress 事件被 Rust 静默丢弃，dashboard 也缺，不止 TUI。
**Decision**: 选范围 3（全做）。分三栈：
1. **Rust 前置**：server.rs nats_event_to_agent_event 加 subtask_progress match arm（+ step_agent/phase/percent 透传）→ 进 broadcast
2. **dashboard 前端**：SubtaskSummary 加 phase/percent/step_agent 字段 + TaskDetail/WorkersPanel 卡片内嵌渲染
3. **TUI**：GrpcBridge 加 watchTask() streaming（抄 useGrpcWeb.ts:210-235）+ OrchestratorEvents 加 subtask_progress 类型 + extension 订阅 + progress-widget/subtask-tree-overlay 渲染 phase/percent/agent
4. **gateway 恢复增强**：容器 healthcheck（compose restart: unless-stopped + healthcheck）+ GrpcBridge 指数退避重连 + 连接状态 UI（dashboard + TUI）
**Consequences**: 跨 Rust/TS/React 四栈，工作量大，拆 PR 渐进交付。Rust 改动是 dashboard + TUI 共同前置。

## Requirements

### Rust（前置，dashboard + TUI 共依赖）
- server.rs `nats_event_to_agent_event` 加 `subtask_progress` match arm，透传 phase/percent/step_agent/step_status/step_summary → 进 broadcast channel

### dashboard 前端
- SubtaskSummary 加字段：phase / percent / step_agent / step_status
- TaskDetail：in_progress subtask 卡片内嵌 phase + percent bar + agent badge
- WorkersPanel：worker 卡片显示当前 subtask 的 agent + phase
- EventLogPanel：突出渲染 subtask_progress 事件（agent + phase + percent 高亮）

### TUI
- GrpcBridge 加 watchTask() streaming（抄 useGrpcWeb.ts:210-235）
- OrchestratorEvents 加 `subtask_progress` 类型（phase/percent/step_agent/step_status/step_summary）
- extension.ts 订阅 subtask_progress + connection_state（后者补 switch handler）
- progress-widget.ts：running subtask 显示 phase + percent + agent（替换单纯 completed/total）
- subtask-tree-overlay.ts：DAG 节点加 agent badge + 当前 phase

### gateway 恢复增强（全套）
- docker compose：gateway 容器加 `restart: unless-stopped` + healthcheck
- GrpcBridge：单次重试 → 指数退避重连（带重连状态：reconnecting + 倒计时）
- 连接状态 UI：dashboard（状态指示 + 最近错误）+ TUI（footer/status-renderer 显示 connected/disconnected/reconnecting）

## Acceptance Criteria

- [ ] Rust: subtask_progress 进 broadcast（grep server.rs 无 catch-all 丢弃）
- [ ] dashboard: in_progress subtask 卡片显示 phase（如 "executing"/"step 2/3: codex"）+ percent + agent badge
- [ ] dashboard: WorkersPanel 显示 worker 当前 agent + phase
- [ ] dashboard: EventLogPanel 突出 progress 事件
- [ ] TUI: GrpcBridge watchTask streaming 订阅事件
- [ ] TUI: progress-widget 显示 running subtask phase/percent/agent
- [ ] TUI: subtask-tree-overlay DAG 节点显示 agent badge
- [ ] gateway 容器挂了自动重启（restart: unless-stopped + healthcheck）
- [ ] GrpcBridge 指数退避重连（长断不再单次 fallback）
- [ ] dashboard + TUI 显示 gateway 连接状态（connected/disconnected/reconnecting + 倒计时）

## Implementation Plan (small PRs)

- **PR1: Rust 前置** — server.rs subtask_progress match arm + 透传字段。dashboard/TUI 共同依赖，先合。
- **PR2: dashboard 前端** — SubtaskSummary 字段 + TaskDetail/WorkersPanel/EventLogPanel 渲染。
- **PR3: TUI 事件管道** — GrpcBridge watchTask streaming + OrchestratorEvents subtask_progress 类型 + extension 订阅。
- **PR4: TUI 渲染** — progress-widget + subtask-tree-overlay 显示 phase/percent/agent。
- **PR5: gateway 恢复** — compose healthcheck/restart + GrpcBridge 退避重连 + 连接状态 UI（dashboard + TUI）。

## Definition of Done

- Tests added/updated
- Lint/typecheck/CI green
- Docs 更新若行为变

## Out of Scope

- 不改后端事件 payload（已齐全）—— 除非 TUI/dashboard 需要额外字段
- 不改 worker 执行逻辑

## Technical Notes

- 后端事件源：python/ultimate_coders/agent/worker.py:649-828, 1010-1110
- dashboard 类型：dashboard/src/types/index.ts:43-70 (SubtaskSummary), 145-165 (DashboardEvent)
- dashboard 组件：TaskDetail.tsx (SubtaskProgressBar:130), WorkersPanel.tsx, EventLogPanel.tsx
- dashboard SSE：useDashboardGrpc.ts:388 (EventSource /dashboard/api/stream)
- TUI 组件：packages/uc-orchestrator/src/ui/{progress-widget,status-renderer,subtask-tree-overlay}.ts
- GrpcBridge：packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts:115-280
- health_monitor：run-omp.sh:189-211（本地有，容器无）
