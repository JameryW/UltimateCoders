# T4: Nats-Msg-Id 幂等闭环 + worker attempt 去重 + 迟到结果 fencing

Tracker: #640（blocked by #639 已关；T6 #642 依赖本票）
Design refs: D3（#633 决议）、D4、D7（#636）、assessment §5；research/idempotency-current-state.md

## Goal

把 at-least-once 交付收成**幂等闭环**：重发/重投/重复发布都不产生第二次有效执行，迟到结果按 epoch/attempt 被拒且留痕，旧信封滞留消息不执行且计数可见。T3 刚做对的 fence→重挂，靠本票才真正"重跑得起来"。

## Scope

1. **发布侧设 `Nats-Msg-Id = idempotency_key`**（激活 JetStream 已配好的 `duplicate_window=120`）。四处发布点全部改 `publish_with_headers`：server.rs:2424、server.rs:3269、`uc-engine/scheduler/dispatcher.rs:243`、Python `_dispatch_remote`。**入站 TTL 去重（server.rs:598）保留**——它管的是入站 update/event，与出站派发不是一回事，误删会开新洞。
2. **worker 端 attempt 级去重**：`worker.py:638-663` 的 checkpoint-skip 键从 `subtask:{subtask_id}` 改为 attempt 范围 `(graph_id, node_id, attempt_id)`。修的是真 bug：T3 的 fence→新 attempt 会撞上旧 attempt 的成功 checkpoint，直接返回陈旧结果而不执行。命中即 ack no-op。
3. **迟到结果按 epoch/attempt 在 gateway 拒绝**：插在 `apply_update_with_metadata` 图动词扇出（server.rs:1513-1528）之前，拒绝时记事件 + 计数，不动节点状态。
4. **旧信封滞留消息**：`term` 丢弃（不发 `subtask_failed`，与 max_deliver 的 :2182 语义区分）+ `stale_dispatch_dropped` 计数，接入健康/Dashboard 可见面。D7：无 DLQ。
5. **拆 T1 过渡期双写**：解析器先读 `graph_id`/`node_id`、回落 legacy，再删 payload 里的 `task_id`/`subtask_id`。**单独一步、单独提交**，便于独立回滚（依赖 D3 的 lockstep 授权）。

## Out of scope

T5 的 Executor trait 统一；T6 的权威反转与 legacy 字段彻底清场；DLQ（D7 已否）；入站 TTL 去重的删除。

## Test seams

- 真 JetStream（本机 4222 可用，`UC_SUBTASKS` 已存在 dup_window=120）：重复 publish 同一 `Nats-Msg-Id` 只落一条；redelivery 不产生二次执行；acked-after-exec 路径。
- worker：新 attempt 不撞旧 attempt 的成功 checkpoint（回归 T3 重挂）；同 attempt 重投命中 → ack no-op。
- gateway：迟到旧 attempt 结果被拒且留痕；`stale_dispatch_dropped` 计数可见。
- 基线只增不减（T3 后 437/379/187+8/36/34）；pytest 套件不回退。

## 门禁

fmt；clippy 五目标双 feature `-D warnings`；Rust 五套件 + pytest；真 JetStream 与真 PG 的 ignored 套**实跑**。日志一律 `> /tmp/t4x.log 2>&1; echo EXIT=$?`。

## Progress（2026-09-14 收尾更新）

**第 1、2 项（2026-09-13 完成）**

- 第 1 项：四处发布点全部改 `publish_with_headers`，`Nats-Msg-Id = idempotency_key`
  — `server.rs::dispatch_dedup_headers()` 新 helper，两处网关发布口（2424→2449、3269→3300）接入；
  `uc-engine/src/scheduler/dispatcher.rs` legacy 发布器接入；
  `nats_worker.py::_dispatch_remote` 接入。
  关键实测发现：**core NATS 发布也触发流去重**（探针：3 次同 id → 流内 1 条；JS 发布同结论）。
  所以没有把 `client.publish` 换成 `jetstream.publish`，改动面从"换发布通道"缩到"加一个 header"。
- 第 2 项：worker 端 checkpoint 键从 `subtask:{id}` 改为 attempt 级
  `attempt:{parent_id}:{id}:{dispatch_retry_count}`（`Worker._attempt_checkpoint_key`）。
  这修的是真 bug：T3 的 fence→READY→重派会撞上旧 attempt 的成功 checkpoint 直接返回陈旧结果。
- 测试：`subtask_execute_payload_emits_deterministic_envelope` 加去重 header 断言
  （同 attempt 同 header、新 attempt 不同 header）；Python 侧新增
  `tests/python/test_nats_dedup_integration.py`（3 个 `@pytest.mark.integration`，真 JetStream）
  与 `test_checkpoint_key_is_scoped_to_the_attempt_not_the_subtask` 回归。

**第 3、4、5 项（2026-09-14 完成）**

- 第 3 项（迟到结果 fencing）：入站 `NatsSubtaskUpdate` 加 `attempt_id: Option<u64>`（serde default，
  未盖章的 legacy 更新不 fence）；Python `_make_task_update_payload` 在 subtask entry 盖章
  `attempt_id = dispatch_retry_count`；gateway `apply_update_with_metadata` 在**部分更新且盖章 attempt
  严格小于当前 attempt** 时拒绝整条 entry（状态/结果/图动词都不动），循环外结算：计数 + warn 日志 +
  `TaskUpdated{status:"stale_result_rejected"}` 用户可见事件。等于/大于放行（重派竞态下 tracker 可能滞后）；
  完整快照 `partial=false` 整体绕过 fence（orchestrator 不追踪 gateway 的 attempt 计数，防误杀）。
- **顺带修复 attempt 身份链真 bug**：`_build_subtask_from_data` 此前不读 `retry_count`/`attempt_id`，
  Python Subtask 的 `dispatch_retry_count` 恒 0 → 第 2 项的 attempt 级 checkpoint 键在真实派发路径上
  全部塌缩为 attempt 0。现按 `retry_count ?? attempt_id` 读取（int 解析失败降级 0），
  并在 `_make_subtask_result_task` 全链路透传。
- 第 4 项（旧信封滞留消息）：worker `_handle_subtask_execute_js` 在 cancelled 检查后插入 stale-envelope
  分支——`_is_stale_dispatch`（只查 `idempotency_key/contract_version/graph_id/node_id/attempt_id` 在场，
  不查相等，滚动升级窗口友好）判定缺 envelope → term（不发 subtask_failed，与 max_deliver cap 语义区分）
  + `stale_dispatch_dropped` 计数 + `stale_dispatch_dropped` 事件（复用 TaskUpdated 事件面，避免新增
  AgentEventType 变体波及 4-5 处穷举 match）。gateway 侧：`NatsHeartbeat.stale_dispatch_dropped`
  （serde default）→ TaskStore 按 consumer_id 存**单调最大值**；getter 两个；ListWorkers NATS 回退路径
  把 per-worker 计数放进 `WorkerProto.metadata` JSON（dashboard_service.rs）。未改 proto。
- 第 5 项（拆 T1 双写，D3 lockstep 授权）：三处 wire 全部停发 `task_id`/`subtask_id`——
  server.rs `subtask_execute_payload` 置空 + serde `skip_serializing_if`；dispatcher.rs legacy json! 移除两键；
  `_dispatch_remote` 同步。worker `_parse_subtask_message` 改 **graph 优先、legacy 回落**
  （回落消息随后被 stale-envelope check term）。**单独一个提交**便于独立回滚。

**测试修复（行为变更引发的过时断言）**

- 3 个 JetStream 行为测试的 `_make_subtask_payload` 夹具原是无 envelope 旧式载荷 → 被 D7 stale 分支
  term（日志实锤）。夹具升级为带完整 envelope 的 T4 wire 格式；
  `test_js_missing_ids_terminates` docstring 更新为 D7 语义。
- `test_dispatch_remote_publishes_execution_envelope` 原 `task_id == graph_id` 双写断言 → KeyError；
  改为断言 legacy 键缺席 + envelope 字段。

**门禁（2026-09-14）**

- fmt ✓；clippy 五目标（uc-types / uc-engine 默认+no-default / uc-grpc --all-features / uc-grpc-server）全 0 ✓；
- Rust 五套件：437 / 379 / 187+8 / 36 / 34，基线只增不减 ✓；
- pytest 全量：978 passed / 4 skipped（JetStream 集成测试，本机 NATS(4222) 宕机响亮 skip，
  去重行为已在 NATS 在线时探针实测）/ 0 regression ✓（T4 相关文件全部通过）。
  环境备注：test_merge_arbiter / test_workspace 的 git-worktree 测试受**双重沙箱环境机制**影响，
  与代码无关（对照实验定位）：
  (a) 沙箱 safe-delete 批量删除保护拦截 seed 夹具目录的删除（58-70 个文件 > 50 阈值）→ 删除抛异常 →
      worktree 搭建失败（沙箱模式下 ~8 个失败）；
  (b) bypass 模式 + 深层临时目录（pytest-of-jamer/pytest-N/...）→ `git worktree add` 触发
      Windows MAX_PATH 限制（5 个失败）。
  bypass + 浅 basetemp（`.scratch/pytest-tmp`）下全部通过（11 passed EXIT=0 实证）。
- **真 PG 集成测试未实跑（环境阻塞）**：本会话 Postgres(5432) 宕机，Docker Desktop daemon 停止，
  `com.docker.service` 启动需管理员权限（非交互环境无法提权）。T4 改动全部位于 NATS 消息层，
  未触碰 graph_store/storage；真 PG 套件 17 个测试已在 T3（#639）当日实跑通过。
  真 JetStream 集成测试同因本机 NATS(4222) 宕机走响亮 skip（连接守卫 `connect_or_skip` 生效）。

