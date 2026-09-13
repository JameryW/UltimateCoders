# T3: 图状态机 + NodeCompletion commit-once + attempt 生命周期

Tracker: #639（blocked by #638 已关；解锁 T4 #640 与 T6 #642 的图面）
Design refs: assessment D1/D6、#635 resolution；research/state-machine-wiring.md

## Goal

让图平面长出**真实的转移逻辑**：CAS 版本推进、commit-once、attempt 超时→fence→READY、依赖重算。挂进网关既有突变点（sink 注入，`UC_GRAPH_SHADOW` 既有门控），**legacy HashMap 行为零变化**——"替换 reaper"的票面措辞按 research §3 裁决为：本票图平面并跑正确逻辑，T6 切主时删 legacy。

## Scope

1. **uc-types `NodeStatus`**（9 态 CREATED/READY/SCHEDULED/RUNNING/WAITING/SUCCEEDED/FAILED/CANCELLED/SKIPPED，字符串 serde）+ 纯函数合法转移表（`can_transition(from,to)`）；lib.rs 显式 `pub use`（E0422 坑）。
2. **uc-engine `graph_store.rs` 扩展（storage feature）转移方法**，每个 = 单事务 + `UPDATE … WHERE version=$n` CAS + 追加 `execution_events` 行（event_type + graph_version；cost/tokens 留 NULL）：
   - `schedule_attempt(node, worker, epoch)`：READY→SCHEDULED→RUNNING 合一（派发即跑），attempt 行 retry_no 取 max+1。
   - `heartbeat_attempt(attempt_id)`。
   - `commit_once(node, winning_attempt, result) -> bool`：`INSERT node_completions ON CONFLICT DO NOTHING`；胜者→node SUCCEEDED + 下游 `recompute_ready`；败者/被 fence 的迟到结果→只记 `late_result` 事件，状态不动。
   - `fail_attempt`：有 retry 预算→attempt FAILED+**epoch bump（fence）**+node 回 READY；耗尽→FAILED。
   - `timeout_sweep`：RUNNING attempt `heartbeat_at` 超阈值→fail_attempt 路径（图平面的"reaper"）。
   - `recompute_ready(graph)`：依赖全部 SUCCEEDED（或 optional SKIPPED）→READY；幂等。吸收 get_ready_subtasks 语义到 node 层，legacy 函数不动。
3. **`GraphShadowSink` 加动词**（always-compiled，新参数全走 T1 envelope）：`on_schedule/on_heartbeat/on_commit/on_fail`；T2 既有 fake sink 与测试跟改。默认不实现者走 no-op default method，防编译面爆炸。
4. **server.rs 挂钩（fire-and-forget，None 零开销）**：publish/dispatch 置 Assigned 后 →`on_schedule`；`apply_update_with_metadata` 终态派生后 →`on_commit`/`on_fail`；reaper 300s 与 stale-worker 路径 →`on_heartbeat`超时→`on_fail`。**只加旁路，legacy 突变/测试(:5260-5600)原样不动。**

## Out of scope

删 legacy reassign（T6）；Nats-Msg-Id/worker 去重（T4）；WAITING/SKIPPED 的运行时产生者（optional 节点策略归 T5 后，本票仅枚举+转移表覆盖）；events cost 列写入方（P2 Optimizer）。

## Test seams

- 纯：9 态转移表全枚举，非法边拒绝。
- 真 PG（`#[ignore]`+visible skip，样板同 T2）：并发双 commit 恰一 winner；commit 后迟到 FAIL attempt 不改态且留 late_result；菱形 A→(B,C)→D(仅依赖 B)：B commit→D READY 不等 C；timeout_sweep→fence(epoch+1)+node 回 READY+可再 schedule；同 version 双 CAS 一胜；recompute_ready 幂等。
- 网关：recording fake sink 断言四动词在 dispatch/update/reaper 路点触发；**T2 前基线 435/377/182+8/36/28 只增不减**（reaper 三测试保持绿=legacy 未动之证）。

## 门禁

fmt；uc-engine（含 --no-default-features）；uc-grpc --all-features；uc-grpc-server；uc-types；clippy 双 feature 模式 `-D warnings`；真 PG ignored 套实跑。日志一律 `> /tmp/t3x.log 2>&1; echo EXIT=$?`。
