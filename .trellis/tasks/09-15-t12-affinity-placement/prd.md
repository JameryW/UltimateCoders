# T12: Affinity placement — per-worker subjects + 网关打分 + shared overflow（#654）

## 背景

D12 #649：派发今天只有 capability 硬闸——JetStream queue 语义下"谁先 claim 谁拿"，
没有分数。C5（T6 #642）刻意留了 affinity 接口：`conflict_risk` 只在分解期给**本地**
并行执行集分级，不参与远端派发。而 affinity 的天然输入（node `file_constraints` ∩
worker 最近工作文件）两侧 wire 上都已经有载体：`file_constraints` 在 dispatch payload
里，worker 侧的"最近文件"只需一个心跳字段。

D12 裁决：**网关侧打分**，且是**软偏好（定向 + 重排），不是硬闸**；claim-time 打分
被否——worker 拒收要 nak，烧掉 durable 的 `max_deliver=5` 预算后节点就丢了，违背 T4
的恢复模型。维度序：capability（硬，不变）> scope 过滤（T8，硬）> file affinity >
load > locality；平局取低负载。

## Scope

1. **per-worker subject**：`uc.subtask.execute.w.{worker_id}`。worker 在绑定共享
   durable 之后**再绑定自己的 per-worker durable**（`filter_subject` 即该 subject），
   成功后在该心跳里声明 `per_worker_topic=true`。共享 `uc.subtask.execute` 作为
   **overflow** 保留——legacy worker（无 per-worker consumer，不声明）只走共享。
   流（`UC_SUBTASKS`）的 subject 列表补 `uc.subtask.execute.w.>`（provisioning）。
2. **心跳字段**：`WorkerHeartbeatRequest` 加 `recent_files`（有界）与
   `per_worker_topic`；registry 存储（归一：trim/去空/去重/截断上限）。
3. **打分**：`placement.rs` 纯函数 + registry `placement_target()`。候选先过
   capability（硬）与 scope（硬）与 contract_version；再算
   `affinity = |file_constraints ∩ recent_files|`（路径分隔符归一为 `/`），
   **阈值 `MIN_AFFINITY_HITS = 1`**：低于阈值（零重叠）→ 直接用共享 subject
   （"没有分数上的理由定向"时，overflow 严格更优）。达标候选之间按
   affinity desc → load_percent asc → locality（与同任务兄弟节点同 host）desc →
   worker_id asc（确定性兜底）排序，取第一名，发布到它的 per-worker subject。
   **locality 的 host 来自注册 metadata 里既有的稳定键 `hostname`**（Python
   `_registration_metadata()` 一直在发），无需新增 wire 字段。
4. **软语义**：打分只决定"先去哪里"——每个节点都仍可经 overflow 派发；
   心跳过期/打分异常一律退化为今天的行为（全走共享），**绝不搁死节点**。
   定向**不写** `assigned_worker`（定向 ≠ 指派），reaper 的 stale-Assigned 语义不变。
5. **测试**：打分单测（维度优先序 + 平局）；定向测试（选中的是最高分 worker 的
   subject）；overflow 回退（无达标者 / legacy worker 未声明）；心跳过期 /
   低于阈值 → 共享 subject 仍派发成功；跨语言 subject 格式 golden（Rust + pytest）。

## 验收（票面）

- `file_constraints` 与 worker A 的 recent files 重叠的节点落在 A 的 subject，而不是
  共享队列（Rust 测试）。
- 所有 worker 过期/低于阈值 → 共享 subject，派发成功。
- legacy worker（无 per-worker consumer）仍能经 overflow 收到工作。

## 非目标

- worker 侧 consumer 由**网关**创建：改为 worker 在注册前自建并声明（跨语言
  consumer-config 耦合 + 本票无 live NATS 集成测试，风险不可控；详见 notes）。
- 打分的历史校准 / 权重学习 / 指标采集；`conflict_risk` 语义变更。
- 定向失败后的重定向（失败一律回落共享 overflow，靠既有重派路径）。
