# PRD: handle_subtask_result 幂等（#3 回环双处理）

## 背景

/loop 第 21 轮，Python 审计 #3（HIGH/M）。default 模式 NatsWorker 订阅 `uc.task.event`，`_handle_task_event` 无来源过滤（既有过滤曾因丢全部事件被删，注释在案 nats_worker.py:1674-1676），而 `Worker._publish_event` 把本地执行结果发到同一 subject → **本地执行的每个 subtask 结果被处理两次**：`_run_one` 直调 `handle_subtask_result` 一次 + 回环事件经 `_handle_remote_subtask_result`（worker_id="remote"）又一次。

后果（已核实 orchestrator.py:196-228）：
1. `entry.current_load -= 1` 双减（`>0` 守卫防负但**偷减并发 subtask 的容量记账**）；
2. `_update_task_status` 二次评估 all-COMPLETED → **`_schedule_arbitration` 双触发** → 两个并发 MergeArbiter 在同一 arbiter clone 做 git merge → 重复/损坏 merge 进 main；
3. worker_id="remote" 伪造结果污染 worker 归属。

## 改（稳健核心 = 幂等，单层修复阻断全部观察到的路径）

`handle_subtask_result`（agent/orchestrator.py:196）：找到 subtask 后，若 `st.status ∈ (COMPLETED, FAILED)` → debug 日志 + return（结果已应用，重复事件忽略）。

为何安全不破重试：retry 流程先把 subtask 重置 PENDING 再执行（状态离开终态），新结果到达时状态是 RUNNING/ASSIGNED → 正常应用。先到先赢：本地 `_run_one` 与回环谁先谁后不定，但两者对同一 subtask 成功/失败语义一致，首个应用即正确终态。

为何单层够：回环是观察到的唯一双发源；幂等后第二份结果整体跳过（load 不减、_update_task_status 不跑、仲裁不触发）。单事件循环内 `all()` 检查与 `_schedule_arbitration` 之间无 await → 无并发竞态双触发。故不加 arbitration scheduled 标志（YAGNI）。

`_handle_remote_subtask_result` 的 intent 移除（conflict_detector.remove_intent）回环时会重复调——remove 幂等（删不存在的 intent 无害），不动。

## 验收

- 新 tests/python/test_orchestrator_result.py：
  - 双发同一成功结果 → load 只减一次（并发另一 subtask 的 load 不被偷减）；
  - merge_arbiter mock + 全完成 → arbitrate 只调度一次（双发后仍 1）；
  - FAILED→retry 语义保留：失败结果应用后，重置 PENDING + 新成功结果正常应用为 COMPLETED。
- pytest tests/python 全绿（本地 + CI ci-python 权威）。
- feature branch + PR + CI green。

## 不做

- 来源标签过滤（consumer_id 穿透 publisher + 全事件面，blast radius 大；幂等已达正确性，归属污染 #3c 仅为 cosmetic——回环结果被跳过即不再覆盖 worker_id……注意：首个若是回环（remote）先到，worker_id="remote" 仍会落——概率低（本地直调先于 NATS 往返），接受）。
- #4 remote 超时（下轮）；#5 dashboard loop；#6 同步 gRPC。
