# PRD: Fix Dashboard Frontend-Backend Interaction Bugs

## Problem

Dashboard 前后端交互存在 9 个问题，从编译阻断到状态竞态到体验缺陷，按优先级分为 P0/P1/P2/P3。

## Issues

### P0 — 编译阻断

1. **合并冲突未解决**: `App.tsx`, `useGrpcWeb.ts`, `useDashboard.ts` 含 `<<<<<<< HEAD` / `>>>>>>> origin/main` 冲突标记，dashboard 无法编译。需选取合理版本并手动合并两边的改动。

### P0 — 状态正确性

2. **SSE snapshot 覆盖 gRPC-Web 增量更新**: SSE 和 gRPC-Web 同时连接时，SSE `onUpdate` 的全量 snapshot 会用可能过时的数据覆盖 gRPC-Web 刚推送的增量更新。`handleSnapshot` 只防了空数组，没防部分过期。修复：snapshot 合并时对 tasks 做 field-level merge，不整体替换。

### P1 — 逻辑缺陷

3. **fetchInitial skipTokens 逻辑无效**: `useEffect` 首次渲染时 `grpcState` 必为 `"disconnected"`，`skipTasks` 永远 `false`。gRPC 后续连接后 REST 已加载，gRPC listTasks 再覆盖 → 闪烁。修复：fetchInitial 不依赖 grpcState，改为 gRPC 连接成功后主动 listTasks + merge（已有该逻辑，去掉 skipTasks 参数）。

4. **pause/resume 独立 transport 残留**: `useGrpcWeb.ts` 旧版 pauseTask/resumeTask 各创建独立 `createGrpcWebTransport()`，不复用共享 transport。删除旧版代码，统一用 `getTransport()`。

5. **broadcast lag 静默丢事件**: gRPC server WatchTask broadcast receiver lagged 时只 warn + continue，客户端不知道丢了事件，UI 卡在旧状态。修复：lagged 时 yield 一个 `sync_required` 类型事件，前端收到后做 listTasks 全量同步。

### P2 — 体验问题

6. **stale 标记只看 SSE 不看 gRPC-Web**: `stale = !connected`（只看 SSE），但 gRPC-Web 连着时数据仍在更新。用户看到 stale 标记但数据在动。修复：`stale = !connected && grpcState !== "connected"`。

7. **status_counts 延迟更新**: 任务状态变化时只有 snapshot（2-5s 间隔）才更新 status_counts，实时事件只更新单个任务。修复：handleTaskEvent 里在状态变更时同步更新 status_counts。

### P3 — 小体验

8. **EventTimeline 只显示 20 条**: 长任务丢失早期事件。修复：改为 50 条 + 增加 "Show more" 按钮。

9. **2s 去重窗口吞合法重复事件**: subtask pause→resume→start 2s 内第二次 `subtask_started` 被去重。修复：去重 key 加入 event 的 data hash（如 description 或 status 字段），使同类型不同内容的事件不被误去重。

## Scope

- 文件：`dashboard/src/App.tsx`, `dashboard/src/hooks/useGrpcWeb.ts`, `dashboard/src/hooks/useDashboard.ts`, `dashboard/src/hooks/useSSE.ts`, `dashboard/src/components/panels/TaskDetail.tsx`, `crates/uc-grpc/src/server.rs`
- 不涉及 proto 变更，`sync_required` 复用现有 TaskEvent.type 字段
- 不涉及 Python 后端变更

## Acceptance Criteria

- [ ] 零合并冲突标记，`npm run build` 通过
- [ ] SSE + gRPC-Web 同时连接时，snapshot 不覆盖增量更新
- [ ] fetchInitial 不依赖 grpcState 初始值
- [ ] pause/resume 使用共享 transport
- [ ] broadcast lag 时客户端收到 sync_required 并做全量同步
- [ ] stale 标记同时考虑 SSE 和 gRPC-Web 状态
- [ ] status_counts 在实时事件中同步更新
- [ ] EventTimeline 显示 50 条，支持展开
- [ ] 去重 key 包含 data hash，不误去重合法重复事件
