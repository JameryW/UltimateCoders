# 全链路优化：事件详情回流、去重、增量推送、自动调度、checkpoint 增强、Event Sourcing、心跳、SSE fallback

## Goal

优化 Orchestrator-Worker-Dashboard/TUI 全链路：让事件详情完整回流到 UI、消除重复事件、减少无效数据传输、自动调度子任务、增强 checkpoint 数据、实现 Event Sourcing 持久化、Worker 心跳超时释放、Dashboard SSE fallback。

## Requirements

### 短期（高 ROI）

1. **Sandbox stdout streaming 事件细化** — 解析 Claude Code 输出中的 `tool_use` / `tool_result` JSON，发射 `tool_call` / `tool_result` 事件到 `_on_stdout_line`
2. **Subtask result 详情回流到 Dashboard** — `SubtaskSummary` 增加 `modified_files`、`retry_count`、`error` 字段；`mergeSubtaskEvent` 写入这些字段
3. **事件去重** — `handleTaskEvent` 按 `task_id + subtask_id + type + timestamp` 去重（5s 窗口），避免 UI 闪烁

### 中期

4. **gRPC WatchDashboard 增量推送** — NATS 模式改为 event-driven（订阅 `uc.task.event`），snapshot 仅做初始同步和周期 reconciliation
5. **Orchestrator 自动调度** — `submit_task` 完成拆分后自动调用 `schedule_ready_subtasks()`；NatsWorker 的 `_handle_submit` 中移除手动调度
6. **Checkpoint 增强** — `_save_checkpoint` 存储 `modified_files`、`tool_calls`（截断到 5 条）、`error`；`_load_checkpoint` 恢复完整 `SubtaskResult`

### 长期

7. **Event Sourcing 持久化** — NATS JetStream 持久化 `uc.task.event` subject + consumer group，Dashboard/gRPC server 重启可 replay
8. **Worker 心跳 + 超时释放** — `_heartbeat_loop` 检测间隔；超时（默认 90s 无心跳）自动释放 subtask + 标记 `SubtaskStatus.PENDING` 重新调度
9. **Dashboard SSE fallback** — React 前端接已有的 `/dashboard/api/stream` SSE 端点，gRPC-Web 不可用时自动降级

### 跨切面

- 事件 schema 加 `v: 1` version 字段，为未来 migration 预留
- 去重逻辑封装为共享函数 `dedupEventKey()`，TUI 侧后补

## Acceptance Criteria

* [ ] Dashboard 实时展示 tool_call / tool_result 事件（EventLogPanel 可见）
* [ ] SubtaskSummary 包含 modified_files、retry_count、error
* [ ] 5s 窗口内重复事件不触发 React re-render
* [ ] gRPC WatchDashboard NATS 模式不再每 30s 推全量 snapshot，改为增量 event + 周期 snapshot
* [ ] submit_task 后 ready subtasks 自动被 assign + execute
* [ ] checkpoint 包含 modified_files、tool_calls、error；resume 后 SubtaskResult 完整
* [ ] NATS JetStream 持久化 uc.task.event；重启后 gRPC server 可 replay 丢失事件
* [ ] Worker 心跳超时后 subtask 自动释放并重调度
* [ ] Dashboard 在 gRPC-Web 连接失败时自动切换 SSE

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes
* Rollout/rollback considered if risky

## Out of Scope

* TUI 侧去重（后续任务补）
* 多租户 / 权限模型
* Dashboard 离线模式 / PWA

## Technical Notes

### 关键文件

| 文件 | 优化点 |
|------|--------|
| `dashboard/src/types/dashboard.ts` | #2 SubtaskSummary 扩展 |
| `dashboard/src/hooks/useDashboard.ts` | #2 mergeSubtaskEvent, #3 去重 |
| `dashboard/src/hooks/useDashboardGrpc.ts` | #9 SSE fallback |
| `python/ultimate_coders/agent/worker.py` | #1 stdout 解析, #6 checkpoint |
| `python/ultimate_coders/agent/orchestrator.py` | #5 自动调度, #7 事件 version |
| `python/ultimate_coders/nats_worker.py` | #5 移除手动调度, #8 心跳超时 |
| `crates/uc-grpc/src/dashboard_service.rs` | #4 增量推送 |
| `crates/uc-grpc/src/local_worker.rs` | #7 WorkerTaskEvent version |
| `python/ultimate_coders/local_worker.py` | #7 ForwardingEventEmitter version |
| `python/ultimate_coders/dashboard/app.py` | #9 SSE 端点已存在 |

### 去重策略

- Key: `${ev.type}:${ev.task_id}:${ev.subtask_id || ""}:${ev.timestamp}`
- 窗口: 5s 内同 key 视为重复
- 实现: LRU Map<string, number>（timestamp），超过 5s 的条目清理

### Checkpoint 增强 schema

```json
{
  "subtask_id": "...",
  "worker_id": "...",
  "summary": "...",
  "success": true,
  "modified_files": [{"file_path": "...", "change_type": "modify"}],
  "tool_calls": [{"name": "read_file", "input_summary": "..."}],
  "error": null
}
```

### Event version

所有 `TaskEvent` 和 `WorkerTaskEvent` 增加 `v: 1` 字段（默认值）。消费者忽略未知 version。

### Worker 心跳超时

- `_heartbeat_loop` 间隔: 30s
- 超时阈值: 90s（3 次未收到心跳）
- 超时动作: 释放 subtask → 标记 PENDING → 重新调度
- `_stale_worker_cleanup_loop` 已存在（`nats_worker.py:1029`），扩展它

### NATS JetStream Event Sourcing

- Subject: `uc.task.event` → JetStream stream `UC_TASK_EVENTS`
- Retention: interest-based, max age 7 days
- Consumer group: `dashboard-replay` for gRPC server
- Replay on startup: from last acked sequence
