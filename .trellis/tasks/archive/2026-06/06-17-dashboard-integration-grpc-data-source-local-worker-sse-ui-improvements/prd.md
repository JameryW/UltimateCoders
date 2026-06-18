# Dashboard integration — gRPC data source + local_worker + SSE + UI improvements

## Goal

让 Dashboard 从两条数据路径（Python FastAPI SSE + Rust gRPC WatchTask）获取任务状态，确保在无 NATS 场景下也能看到 local_worker 执行的任务，同时改善 Dashboard UI 体验。

## What I already know

* Dashboard 前端已实现 gRPC-Web 客户端（`useGrpcWeb.ts`），通过 WatchTask server-streaming 订阅 gRPC 事件
* SSE + gRPC-Web 双通道已通过事件去重（`seenKeys` Map + 2s 窗口）合并展示
* 无 NATS 时，gRPC-Web WatchTask 是 local_worker 事件的唯一前端通道（已覆盖）
* `handleSnapshot` 增加 `available` 守卫，SSE 空 snapshot 不再覆盖 gRPC-Web 填充的数据
* gRPC-Web 手动重连始终可用（retryCountRef 在 connect() 时重置）
* SSE 断开自动重连已实现（exponential backoff 1s-16s）
* SSE 可靠性增强：cancellable_sleep（0.5s 检测断连）、event id、heartbeat comment

## Resolved Open Questions

* **数据路径选择**：gRPC-Web WatchTask — 前端已直接消费，无需 Python/Rust 侧桥接
* **SSE 桥接位置**：不需要 — gRPC-Web 已在前端与 SSE 并行工作
* **UI 改善范围**：lastUpdate 时间戳、SearchPanel 断连提示、transport 复用

## Requirements

* ~~Dashboard 能看到 gRPC/local_worker 提交的任务~~ ✅ gRPC-Web WatchTask
* ~~两条路径（FastAPI SSE + gRPC）的事件能合并展示~~ ✅ 事件去重
* ~~连接断开时有优雅降级~~ ✅ SSE auto-reconnect + gRPC manual reconnect

## Acceptance Criteria

* [x] Dashboard 实时显示 local_worker 执行的任务及事件（gRPC-Web WatchTask streaming）
* [x] 无 NATS 时 Dashboard 仍能工作（gRPC-Web fallback + SSE snapshot guard）
* [x] SSE 断开自动重连（exponential backoff + event id + heartbeat）

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Out of Scope (explicit)

* Python gRPC subscriber（前端 gRPC-Web 已覆盖 no-NATS 场景）
* Rust HTTP/SSE bridge（不需要 — gRPC-Web 已直连）
* 全局 InteractionLog 面板（当前 EventLogPanel 已足够）

## Technical Notes

* gRPC-Web hook: `dashboard/src/hooks/useGrpcWeb.ts` — WatchTask streaming, submitTask, listTasks, healthCheck
* SSE hook: `dashboard/src/hooks/useSSE.ts` — EventSource with exponential backoff reconnect
* Dashboard state: `dashboard/src/hooks/useDashboard.ts` — handleSnapshot guard, mergeGrpcTasks
* Event dedup: `dashboard/src/App.tsx` — seenKeys Map with 2s window + 60s pruning
* Python SSE: `python/ultimate_coders/dashboard/app.py` — cancellable_sleep, event id, heartbeat
