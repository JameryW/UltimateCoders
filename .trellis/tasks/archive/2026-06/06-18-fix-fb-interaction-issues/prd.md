# Fix Frontend-Backend Interaction Issues

## Goal

修复前后端交互中的 7 个逻辑/体验问题，提升数据一致性、连接可靠性、用户反馈质量。

## Requirements

### P0 — TUI offline mock status 大小写不一致
* `simulateOfflineSubmit` 生成的 mock subtask status 用 `'InProgress'` / `'Pending'`（PascalCase）
* `mapSubtaskStatus` 和 reducer 期望 `'in_progress'` / `'pending'`（snake_case）
* 修复：offline mock 使用 snake_case，与 gRPC proto 和 reducer 保持一致

### P1 — `processEvent` 忽略未知 subtask（不创建）
* TUI `useTaskEvents.ts` 中 `subtask_assigned` / `subtask_started` 事件到达时，如果 subtask 不在 map 里就跳过
* Dashboard 端 `mergeSubtaskEvent` 会创建新条目
* 修复：TUI 侧也应在收到事件时创建 subtask 条目（与 Dashboard 行为一致）

### P1 — SSE snapshot 可能覆盖同秒内 gRPC 增量更新
* `handleSnapshot` 用 `updated_at` 时间戳比较，秒级精度不够
* 同秒内 gRPC 增量事件可能被 snapshot 覆盖，导致 subtask 状态回退
* 修复：timestamp 相等时，优先保留 reducer 中的现有版本（即 gRPC 增量 > SSE snapshot）

### P1 — `mergeGrpcTasks` 浅合并丢失 subtask 状态
* `{ ...merged[idx], ...t }` 浅 spread，如果 gRPC listTasks 返回的 task 没有 subtasks，会覆盖掉已有 subtasks
* 修复：深度合并 subtask 字段，保留更完整的版本

### P2 — SSE 事件去重 2s 窗口误杀合法事件
* `dedupedHandleTaskEvent` 用 2s 窗口 + dataHash 去重
* 应改用 event id 幂等（SSE 已提供 `id` 字段），而非时间窗口
* 修复：基于 event id 去重，2s 窗口仅作为 fallback

### P2 — 双通道初始加载闪烁 + stale 判断过于二元
* fetchInitial + gRPC listTasks 同时触发，面板内容闪烁
* SSE 断开但 gRPC 连接时，所有面板标记 stale（实际 gRPC 数据仍活）
* 修复：
  - gRPC connected 时跳过 REST fetchInitial 的 tasks 部分
  - stale 按数据源细分：SSE-only 数据（health, workers, scheduler, CB）标记 stale，gRPC 数据不标记

### P2 — 提交后无即时反馈 + ChatLog 截断无提示
* Dashboard submit 成功后 task 不立即出现在面板
* ChatLog 2000 条截断无提示
* 修复：
  - submit 成功后乐观插入 task 条目
  - 截断时显示 "Earlier messages truncated" 提示

## Acceptance Criteria

* [ ] TUI offline mock subtask status 使用 snake_case，`mapSubtaskStatus` 正确映射
* [ ] TUI `processEvent` 在收到 `subtask_assigned`/`subtask_started` 时创建新 subtask 条目
* [ ] Dashboard `handleSnapshot` timestamp 相等时保留 reducer 现有版本
* [ ] Dashboard `mergeGrpcTasks` 深度合并 subtask 字段
* [ ] Dashboard 事件去重基于 SSE event id
* [ ] gRPC connected 时跳过 REST fetchInitial 的 tasks
* [ ] stale 按数据源细分（SSE-only vs gRPC）
* [ ] Dashboard submit 后乐观插入 task 条目
* [ ] ChatLog 截断时显示提示信息
* [ ] 所有现有测试仍然通过

## Definition of Done

* Lint / typecheck / CI green
* 修改涉及的所有文件有对应测试覆盖（至少关键路径）
* 无新增 console.error / warning 在正常流程中

## Out of Scope

* ConnectionIndicator UI 重设计（合并按钮）— 单独任务
* SSE heartbeat interval 调整 — 不在本次范围
* gRPC AbortController 取消 pending submit — 后续优化
* `task_paused`/`task_resumed` subtask-level 状态传播 — 需要后端支持

## Technical Notes

* 涉及文件：
  - `tui/src/components/App.tsx` — offline mock status
  - `tui/src/hooks/useTaskEvents.ts` — processEvent 创建 subtask
  - `dashboard/src/hooks/useDashboard.ts` — handleSnapshot, mergeGrpcTasks
  - `dashboard/src/App.tsx` — dedup, stale, fetchInitial
  - `dashboard/src/hooks/useSSE.ts` — event id 传递
  - `dashboard/src/hooks/useGrpcWeb.ts` — 无直接改动
  - `dashboard/src/components/forms/TaskSubmitForm.tsx` — 乐观更新回调
  - `tui/src/reducer.ts` — ChatLog truncation hint

* 关键约束：
  - SSE event id 是 monotonic integer（app.py `event_id`），每次 yield 递增
  - gRPC-Web 没有内置 event id，需用 timestamp+type+taskId 作为 composite key
  - `mapSubtaskStatus` 函数在 `tui/src/grpc/types.ts` 中，已处理 snake_case
