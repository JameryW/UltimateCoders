# Worker 失败重分配

## Goal

当远程 Worker 失联（heartbeat 超时）时，自动将其 in-progress subtask 重新分配给其他 Worker 或本地执行，避免 subtask 永久卡死。

## What I already know

- Rust `spawn_heartbeat_monitor` 每 30s 检查 Worker heartbeat，调用 `mark_stale_workers` + `reassign_stale_subtasks` + `dispatch_ready_subtasks`
- Python `_stale_worker_cleanup_loop` 每 60s 清理 90s 无 heartbeat 的 Worker，但之前没有重新分配 subtask
- Subtask 有 `retry_count` 字段但始终为 0
- OrchestratorConfig 有 `max_retries = 3`

## Requirements

### R1: 远程 Worker 死亡时重分配 subtask

- `_stale_worker_cleanup_loop` 检测到 Worker 失联时，将其 assigned subtask 重置为 Pending
- `_dispatch_event.set()` 唤醒 `_execute_subtasks`，重新分配这些 subtask

### R2: subtask retry 限制

- subtask 失败/重分配时递增 `retry_count`
- `retry_count >= max_retries` 时标记 subtask 为 Failed（不再重试）
- `_execute_subtasks` 分配前检查 retry_count

## Acceptance Criteria

- [ ] 远程 Worker 90s 无 heartbeat 后，其 in-progress subtask 重置为 Pending
- [ ] 重分配后 `_dispatch_event.set()` 唤醒调度循环
- [ ] subtask retry_count 超过 max_retries 时标记为 Failed
- [ ] 现有测试通过

## Definition of Done

- Lint / typecheck green
- 不改 proto，不改 Rust 端

## Out of Scope

- 本地 Worker 进程崩溃检测（Rust 侧 `mark_tasks_failed_on_worker_death` 已处理）
- 指数退避重试
- 跨任务 Worker 容量感知

## Technical Notes

- 关键文件: `python/ultimate_coders/nats_worker.py` (_stale_worker_cleanup_loop, _execute_subtasks), `python/ultimate_coders/agent/orchestrator.py`
- Rust 侧 failover 已完整，本次只补 Python 侧
