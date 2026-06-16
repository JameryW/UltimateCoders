# Dashboard integration — gRPC data source + local_worker + SSE + UI improvements

## Goal

让 Dashboard 从两条数据路径（Python FastAPI SSE + Rust gRPC WatchTask）获取任务状态，确保在无 NATS 场景下也能看到 local_worker 执行的任务，同时改善 Dashboard UI 体验。

## What I already know

* Dashboard 前端只走 REST/SSE -> Python FastAPI，没有 gRPC 客户端
* TUI 走 gRPC -> Rust server，有 WatchTask 流式订阅
* 两条路径在无 NATS 时不互通 — Dashboard 看不到 gRPC/local_worker 提交的任务
* Rust gRPC server 已有 `WatchTask` server-streaming RPC（broadcast channel, capacity 256）
* Local worker bridge 已实现（JSON-RPC over stdin/stdout），仅 gRPC server 内部使用
* Python SSE 端点已完善（task_event + update 混合推送）
* gRPC server 的 Health RPC 已包含 local_worker 组件状态

## Assumptions (temporary)

* Dashboard 前端无法直接消费 gRPC（浏览器不支持 gRPC）
* 需要在 Rust 侧或 Python 侧加一个 HTTP/SSE 桥接层
* Local worker 任务事件需要广播到 Dashboard 可达的通道

## Open Questions

* **数据路径选择**：Dashboard 接入 gRPC 数据的方式？
* **SSE 桥接位置**：在 Rust 侧加 HTTP SSE，还是在 Python 侧订阅 gRPC 再转发？
* **UI 改善范围**：具体哪些 UI 改善？

## Requirements (evolving)

* Dashboard 能看到 gRPC/local_worker 提交的任务
* 两条路径（FastAPI SSE + gRPC）的事件能合并展示
* 连接断开时有优雅降级

## Acceptance Criteria (evolving)

* [ ] Dashboard 实时显示 local_worker 执行的任务及事件
* [ ] 无 NATS 时 Dashboard 仍能工作
* [ ] SSE 断开自动重连

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Out of Scope (explicit)

* (待定)

## Technical Notes

* gRPC server: `crates/uc-grpc/src/server.rs` — WatchTask streaming, broadcast channel
* Local worker bridge: `crates/uc-grpc/src/local_worker.rs`
* Python SSE: `python/ultimate_coders/dashboard/app.py` — `/dashboard/api/stream`
* Dashboard SSE hook: `dashboard/src/hooks/useSSE.ts`
* Dashboard state: `dashboard/src/hooks/useDashboard.ts`
* Proto: `crates/uc-grpc/proto/engine.proto` — TaskService with WatchTask
* Dashboard spec: `.trellis/spec/backend/dashboard-spec.md`
