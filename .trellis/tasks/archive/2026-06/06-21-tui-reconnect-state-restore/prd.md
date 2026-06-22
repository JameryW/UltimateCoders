# TUI 重连后状态恢复

## Goal

TUI 的 WatchTask gRPC stream 断线重连或 broadcast lag 时，客户端丢失事件，task/subtask 状态与服务器不一致。需要在检测到 `sync_required` 或重连成功时，通过 `ListTasks` RPC 全量同步恢复状态。

## Requirements

1. TUI App 组件接收 `onSyncRequired` 回调，触发全量 `listTasks` 同步
2. 同步结果覆盖当前 task/subtask 状态（而非追加）
3. 重连成功后（connectionState 从非 connected 变为 connected）也触发一次同步

## Acceptance Criteria

- [ ] `sync_required` 事件触发后，TUI 在 2s 内完成 listTasks 同步
- [ ] 重连成功后自动同步，无需用户手动操作
- [ ] 同步期间不闪烁/不丢失用户正在查看的 activeTaskId

## Definition of Done

* TypeScript 编译通过
* 手动测试：断开 gRPC server → 重启 → TUI 自动恢复
* 行为与 Dashboard 侧的 sync_required 处理一致

## Out of Scope

* 双状态源统一（#6）
* Dashboard 侧改进（已有 sync_required 处理）
* 改 gRPC proto 或 Rust 服务端

## Technical Notes

* 关键文件: `tui/src/components/App.tsx`, `tui/src/hooks/useTaskEvents.ts`
* Dashboard 参考实现: `dashboard/src/App.tsx:184-193` (sync_required → listTasks → mergeGrpcTasks)
* TUI 已有 `listTasks` 方法从 `useGrpcClient` 返回
* TUI reducer 已有 `SET_TASKS` action 可以批量替换任务列表
