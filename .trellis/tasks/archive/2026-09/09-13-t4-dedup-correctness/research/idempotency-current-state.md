# Research: 幂等闭环现状（T4 前置勘察，HEAD b7506b33，2026-09-13）

## 0. 本机 JetStream 实测可用（2026-09-13）

`127.0.0.1:4222` core NATS 通；`UC_SUBTASKS` 流已存在，`duplicate_window=120.0`、`retention=workqueue`。
结论：票面"真 JetStream 集成测试"**能实跑**，不必退回 skip。驱动用项目 `.venv`（`./.venv/Scripts/python.exe`），
默认 `python` 没有 `nats` 模块。

## 0.2 实测：core NATS 发布也能触发流去重（关键，省掉一次大改）

探针流（workqueue + duplicate_window=120）各发 3 条同 `Nats-Msg-Id`：

| 发布方式 | 3 次同 id 后流内条数 |
|---|---|
| core `nc.publish(headers={Nats-Msg-Id})` | **1** |
| JS `js.publish(headers={Nats-Msg-Id})` | **1**（总数 1→2） |

结论：去重是**流侧**行为，只要消息被流捕获就认 header，与发布端走不走 JS context 无关。
→ 网关**不必**把 `client.publish` 改成 `jetstream.publish`，只加一个 header 即可，改动面从"换发布通道"缩到"加 header"。

## 1. 发布侧：`Nats-Msg-Id` 从未被设置

- `NatsSubtaskExecute`（server.rs:226-295）已带全套 T1 信封字段：`graph_id` / `node_id` / `attempt_id` / `idempotency_key` / `worker_epoch` / `contract_version`，外加 legacy `task_id` / `subtask_id` / `message_id`。
- `subtask_execute_payload()`（server.rs:313-351）构造 `message_id = "{task_id}:execute:{subtask_id}:{now_millis}"` —— **毫秒时间戳，重发即变**，做不了去重键；`idempotency_key` 才是稳定的（sha256 截 32 位十六进制，envelope.rs:76）。
- `uc.subtask.execute` 的发布点共四处，**全部是裸 `.publish(subject, payload)`，没有任何 header**：
  - server.rs:2424（`publish_ready_subtasks`）
  - server.rs:3269（`dispatch_ready_subtasks`）
  - `crates/uc-engine/src/scheduler/dispatcher.rs:243`（legacy scheduler 发布器）
  - Python `_dispatch_remote`（nats_worker.py）
- `async-nats = "0.38"`（根 Cargo.toml）；改用 `publish_with_headers` 即可带 `Nats-Msg-Id`。

## 2. 流已就绪：`duplicate_window=120` 早就配好了

- UC_SUBTASKS 由 Python 侧创建：`nats_worker.py:879-887`，`retention="workqueue"`、`duplicate_window=120`（2 分钟）。
- 也就是说 JetStream 的去重窗口**已经备好**，缺的只是发布方从来不填 `Nats-Msg-Id` → 窗口一直空转。这是 T4 第 1 项最直接的收益。

## 3. 网关入站去重（现有，不是本票要删的那个）

- `check_and_record_message_id`（server.rs:598-607）+ 内存 TTL map（server.rs:475），用于**入站**的 update/event（2709 / 2833 / 2899）。
- 票面"删除内存 TTL 去重作为唯一保障"指的是**出站派发**不能只靠它——它根本不覆盖派发。结论：入站 TTL 保留作第二道，出站以 `Nats-Msg-Id` 为准，不要误删。

## 4. worker 端去重：粒度错了（真实 bug）

- `worker.py:638-663`：进入执行前 `checkpoint = await self._load_checkpoint(subtask.id)`，命中且 `success` 就直接返回存档结果、跳过执行。
- 键是 `subtask:{subtask_id}`（worker.py:983），**只有 subtask 维度，没有 attempt 维度**。
- 与 T3 的因果冲突：T3 的 fence→READY→重派会产生**新 attempt**；新 attempt 撞上旧 attempt 留下的成功 checkpoint → 直接返回陈旧结果、根本不执行。T3 刚把重挂做对，T4 不修这里等于白做。
- 票面要求改为 attempt 范围判定：`(graph_id, node_id, attempt_id)` 已有结果 → ack no-op。

## 5. worker 对旧信封的容忍（T4 要改成拒绝）

- `nats_worker.py:2257-2266`：缺 `idempotency_key` 只记 `logger.info`，照样执行——注释明写"rejection is T4's job"。
- `_parse_subtask_message`（nats_worker.py:2234-2274）要求 `task_id` + `subtask_id` 非空（:2253），否则丢弃。
- `max_deliver` 上限已有（nats_worker.py:2182-2202）：term-ack + 发 `subtask_failed`。T4 的 `stale_dispatch_dropped` 应复用同一 term 语义，但不重复发 failed。

## 6. 迟到结果与计数器

- 入站结果经 `apply_update_with_metadata`，图动词在 server.rs:1513-1528 扇出（T3 已挂）。T4 的"按 epoch/attempt 拒绝"要插在这里之前/之内。
- **`stale_dispatch_dropped` 计数器不存在**：全仓 grep 无 `stale_dispatch`。需要新建并接到健康/Dashboard 可见面（票面 D7 要求，无 DLQ）。

## 7. T1 过渡期双写的删除风险

- 删 `task_id` / `subtask_id` 会直接打断 `_parse_subtask_message` 的 :2253 校验——必须先让解析器优先读 `graph_id` / `node_id` 并回落 legacy，才能拆线。
- D3 已决"一刀切新契约（无兼容窗口）"+ gateway/worker lockstep 升级，所以删除本身有授权；但必须**放在最后一步单独提交**，便于单独回滚。
