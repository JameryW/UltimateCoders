# PRD: NATS Subtask Callback 异步派发（#2 HIGH）

## 背景

/loop 第 20 轮，Python 审计 #2（HIGH/S）。nats-py 2.15 每订阅一个 `_wait_for_msgs` 任务，**内联 await callback**（nats/aio/subscription.py:310 核实）。`_handle_subtask_execute` 在 callback 内 await `execute_subtask`（最长 600s + 3× 重试）→ worker 严格串行处理 `uc.subtask.execute`——`_exec_semaphore(max_capacity)` 与广播的 `max_capacity=3` 全是死码。后续 queue-group 投递堆积在订阅 pending 队列，超 `pending_msgs_limit` nats-py 静默丢弃（仅 debug error_cb）→ **subtask 永失**（core NATS 无 ack/重投）→ 任务永卡 IN_PROGRESS（叠加 #4 无超时）。

## 改

callback 瘦身为快速分派器，执行移入后台任务：

1. `_handle_subtask_execute` 保留：parse + 必填校验 + worker 检查 + capability 检查（NACK/reject 快路径，publish_event 轻量）+ Subtask 构建——全部毫秒级。
2. 执行块（semaphore + execute_subtask + 失败/结果 publish + 日志，现 L1552-1617）整体移入新私有协程 `_execute_and_report(subtask)`，callback 末尾 `self._spawn_bg(self._execute_and_report(subtask))` 即返回。
3. semaphore 留在 `_execute_and_report` 内——现在才真正限流（callback 瞬间释放订阅 reader，pending 队列不再堆积 → 消除静默丢消息主因）。

`_spawn_bg` helper 已存在（L989-999，强引用防 GC 回收 + done-callback 清理）且被 stop() 的 `_bg_tasks` cancel 循环覆盖 → **附带修复 #10 的一半**：shutdown cancel 现在真能到达执行体，触发 sandbox kill-on-cancel（sandbox.py:567-582）。

## 验收

- tests/python 新用例：slow execute（Event 门控）→ callback `wait_for` 快速返回且执行已开始未完成（后台跑）；释放后 bg 任务完成 publish。
- pytest tests/python 全绿（本地 + CI ci-python Py3.9/3.12 权威）。
- feature branch + PR + CI green。

## 不做

- pending_msgs_limit 调优/监控（callback 瘦身后堆积主因消除，限额保持默认）。
- #4 remote 超时下轮；#3 回环双处理下轮（独立修复）；#10 剩半（stop 弃单 publish subtask_failed）随 #4 一起。
