# PRD: Remote Subtask Timeout + Stop 弃单上报（#4 HIGH + #10 剩半）

## 背景

/loop 第 22 轮，Python 审计 #4（HIGH/M）+ #10 剩半。remote 派发 `assign_subtask(subtask, "remote")` 后，唯一恢复路径 `_stale_worker_cleanup_loop` 遍历 `_known_remote_workers`——**"remote" 从不是其键** → 结果事件丢失的 subtask 永卡 ASSIGNED，任务永 IN_PROGRESS，操作者无感知。

丢结果的现实路径（已核实）：执行 worker 在执行与 publish 之间崩溃；`NatsPublisher._publish` 吞所有 publish 异常仅 warning；`stop()` 10s drain 后强关，弃 worker 模式在飞执行**不发 subtask_failed**；`_dispatch_remote` 的 `await self._nc.publish(...)` NATS 失败时异常直抛（subtask 已 ASSIGNED）。

## 改

### F56: remote dispatch 超时回收

1. `__init__`：`self._remote_dispatched_at: dict[str, datetime] = {}`（subtask_id → 派发时刻）。
2. `_dispatch_remote`：assign 后记时间戳；`nc.publish` 包 try/except——失败 → 清时间戳 + `_reset_subtask_to_pending(subtask.id)`（ASSIGNED→PENDING 可被重选）+ warning 日志 + return（不抛）。
3. `_reset_subtask_to_pending`（公共重置点）：pop 时间戳。
4. `_handle_task_event` 的 subtask_completed/failed 分支：pop 时间戳（结果到达，计时终止）。
5. 新私有方法 `_reclaim_timed_out_remote_subtasks()`（从 `_stale_worker_cleanup_loop` 每轮调用，可测）：遍历活动任务，ASSIGNED 且 assigned_worker=="remote" 且 `elapsed > (timeout_seconds or 600) + 60s grace`（无时间戳视同超时——本会话派发记录不应缺）→ PENDING + retry_count++ + publish `subtask_retrying`（reason=remote_timeout）+ 唤醒 dispatch。模式对齐既有 self-stall 块。

### F57: stop() 弃单上报（#10 剩半）

stop() 在 bg 任务 cancel 之后、unsubscribe/drain 之前：对本 worker 名下 RUNNING/ASSIGNED subtask——本地 `handle_subtask_result` 应用失败结果（可靠，F55 幂等防回环双处理）+ publish `subtask_failed`（error="worker shutting down"，通知 gateway/他方）。default 模式 orchestrator 在、worker 模式 orchestrator None 自动跳过。

## 验收

- tests/python 新用例：
  - 超时回收：ASSIGNED-remote subtask + 时间戳 700s 前 → 回收后 PENDING + retry_count 1 + 时间戳清除；未到期的不动。
  - dispatch publish 失败 → subtask 回 PENDING（不卡 ASSIGNED）。
- pytest tests/python 全绿（本地 + CI ci-python 权威）。
- feature branch + PR + CI green。

## 不做

- #5 dashboard NATS loop 绑定（下轮 HIGH）；#6 同步 gRPC 阻塞；#8-#15 杂项。
