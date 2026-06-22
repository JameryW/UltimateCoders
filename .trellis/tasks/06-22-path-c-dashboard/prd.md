# 事件管道全链路优化: Path C真执行 + 子任务结果回传 + Dashboard细粒度事件

## Goal

修复事件管道三条核心缺陷：(1) Path C（无NATS无Worker）是假执行，任务瞬间"完成"但实际没跑；(2) 子任务结果只回传summary一行文本，代码变更和执行输出丢失；(3) 无NATS时Dashboard只有5s粗粒度snapshot，缺少细粒度task_event。

## Requirements

### R1: Path C 真执行 — 换行拆分 + 逐个 `claude -p`
- `submit_task_local()` 保持换行拆分，但对每个子任务 spawn `claude -p "<subtask_description>"` 真实执行
- 串行执行（避免多 claude 进程资源竞争）
- 每个 subtask: emit SubtaskStarted → spawn claude → 收集 stdout → emit SubtaskCompleted(带 result)
- 如果 `claude` CLI 不可用 (which claude fails)，标记 `simulated: true` 到事件 data，保持现有模拟行为
- 执行超时: 默认 120s per subtask，可配置
- stdout 截断到 50KB

### R2: 子任务结果回传 (50KB limit)
- `AgentEventType::SubtaskCompleted` 增加 `result: String` 字段（完整输出摘要）
- NATS `subtask_completed` event data 增加 `result` 字段
- Worker JSON-RPC `WorkerSubtaskUpdate` 增加 `result: Option<String>` 字段
- `SubtaskProto` proto 增加 `optional string result = 7` 字段
- Python `_publish_event("subtask_completed")` data 增加 `result` key
- Dashboard `SubtaskSummary.result` 已有字段，确保后端填充
- Dashboard `TaskDetail` 面板显示 ↳ result 文本（已有 summary 显示逻辑，扩展即可）
- 50KB 上限：Rust 端截断，Python 端截断

### R3: Dashboard 细粒度事件 — 复用 event_tx
- `GrpcServer` 将 `event_tx` receiver 传给 DashboardService（新增字段）
- `WatchDashboard` `#[cfg(not(messaging))]` 路径：同时监听 event_rx + 定期 snapshot
  - event_rx 收到 TaskEvent → 直接 yield Ok(DashboardSnapshot { recent_task_events: [event], .. })
  - 每 5s yield 一次完整 snapshot（workers + tasks + health 等）
- `DashboardSnapshot` proto 增加 `repeated TaskEvent recent_task_events = 8`
- Dashboard 前端 `useDashboardGrpc` 从 snapshot.recent_task_events 提取事件，feed 给 useDashboard.handleTaskEvent
- NATS 路径不变（已有 NATS subscriber）

## Decision (ADR-lite)

**Context**: 三个优化方向各有多条实现路径
**Decisions**:
1. Path C → 换行拆分 + 逐个 `claude -p`（而非整体执行），保留子任务粒度和进度可见性
2. Dashboard 事件 → 复用 event_tx broadcast channel（而非新 gRPC stream），最小 proto 变更
3. 结果大小 → 50KB 上限，平衡信息量和消息体积
**Consequences**: Path C 串行执行可能慢，但比模拟好；event_tx 复用需注意 broadcast channel 容量

## Acceptance Criteria

- [ ] Path C 提交任务后，Dashboard 显示 Planning → InProgress → Completed 真实时序
- [ ] Path C 有 `claude` CLI 时真实执行，无 CLI 时标记 simulated
- [ ] 子任务完成后，Dashboard TaskDetail 面板显示 result 文本
- [ ] FileModified 事件的 diff 从 Python Worker 一路到达 Dashboard
- [ ] 无NATS模式下，Dashboard EventLog 显示 SubtaskStarted/Completed 等细粒度事件
- [ ] `cargo test -p uc-grpc` 通过
- [ ] `cargo check -p uc-grpc --features messaging` 通过
- [ ] Dashboard TypeScript: type-check clean

## Definition of Done

- Tests added/updated for new code paths
- Lint / typecheck / CI green
- Proto changes reflected in both Rust and TypeScript generated code
- No regressions in NATS-enabled path

## Out of Scope

- 多 Worker 分布式调度（已有 06-22-worker 任务）
- NATS-enabled 路径的事件管道变更（已工作正常）
- TUI 侧的额外 UI 改进
- 子任务 stdout/stderr 全量回传（只回传摘要 + truncated）

## Technical Notes

### 关键文件
- `crates/uc-grpc/src/server.rs` — submit_task_local, submit_task_via_bridge, handle_worker_notification
- `crates/uc-grpc/src/dashboard_service.rs` — watch_dashboard
- `crates/uc-grpc/src/local_worker.rs` — WorkerTaskUpdate, WorkerTaskEvent
- `crates/uc-engine/src/task_store.rs` — decompose_task, submit_task
- `crates/uc-types/src/agent.rs` — AgentEventType variants (SubtaskCompleted needs result field)
- `proto/engine.proto` — DashboardSnapshot, SubtaskProto, TaskEvent
- `python/ultimate_coders/agent/orchestrator.py` — handle_subtask_result, _publish_event
- `python/ultimate_coders/local_worker.py` — JSON-RPC worker bridge
- `python/ultimate_coders/nats_worker.py` — NATS worker subscriber
- `dashboard/src/hooks/useDashboardGrpc.ts` — WatchDashboard 消费
- `dashboard/src/hooks/useDashboard.ts` — handleTaskEvent
- `dashboard/src/types/dashboard.ts` — SubtaskSummary, DashboardSnapshot
- `dashboard/src/components/panels/TaskDetail.tsx` — result display

### 实施计划

**Step 1: Proto + Types 变更**
- `proto/engine.proto`: SubtaskProto +result, DashboardSnapshot +recent_task_events
- `crates/uc-types/src/agent.rs`: SubtaskCompleted +result
- `crates/uc-grpc/src/local_worker.rs`: WorkerSubtaskUpdate +result
- `dashboard/src/types/dashboard.ts`: DashboardSnapshot +recent_task_events

**Step 2: R2 — 子任务结果回传 (全链路)**
- Python: _publish_event("subtask_completed") +result, handle_subtask_result 传 result
- Rust: nats_event_to_agent_event("subtask_completed") +result, handle_worker_notification 提取 result
- Rust: SubtaskProto 映射 +result
- Dashboard: useDashboardGrpc mergeSubtaskEvent +result, TaskDetail 显示

**Step 3: R1 — Path C 真执行**
- submit_task_local 改为异步串行 spawn `claude -p`
- 检测 claude CLI 可用性
- 不可用时标记 simulated
- 每个 subtask 真实 emit 事件

**Step 4: R3 — Dashboard 细粒度事件**
- GrpcServer 传 event_tx 给 DashboardService
- watch_dashboard no-NATS 路径: event_rx + snapshot interval
- Dashboard 前端: snapshot.recent_task_events → handleTaskEvent
