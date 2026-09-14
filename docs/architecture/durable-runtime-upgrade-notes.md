# Durable Runtime 升级说明（T6 / #642 交付）

> 状态：**随 T6 #642 交付**。本文是 D7 决议（2026-09-11）要求的发布说明四要点，
> 外加 C1 交付的 reaper 窗口语义合并记录。背景与取舍见
> `durable-runtime-migration-assessment.md`（D1–D7 决议原文在各决策票）。
> 适用版本：T6 之后的所有镜像（graph runtime 成为唯一权威，TS wave 机器已删除）。

## 1. 升级顺序：gateway 与 worker 必须在同一 lockstep 窗口内重启

- **没有灰度路径。** D3 一刀切新契约：派发 wire 只认 graph envelope（含
  `contract_version`），gateway 与 worker 必须同时换新镜像。
- 旧 worker 不是"降级运行"而是**拒绝注册**：注册握手对 capability 与
  `contract_version` 做硬门禁（T1 #637，`uc_types::CONTRACT_VERSION`），
  版本不匹配的 worker 无法进入 registry，也不会收到派发。
- 推荐动作：**一次 lockstep 滚动重启**（gateway 与全部 worker 同窗口）。
  窗口期间的语义见第 2 节——不需要 drain gate，也不需要人工暂停闸。

## 2. 升级窗口内的 in-flight 语义：未 commit 从零重跑，已 commit 永不丢失

- 窗口开启时仍在 RUNNING 的 attempt 不会被打断式清理：它们按 T3 的
  attempt 超时规则被 graph-plane `timeout_sweep` 处理——**fence（epoch 递增）
  → 节点回 READY → 以新 envelope 重派**。半途未 commit 的工作**从零重跑**
  （commit-as-truth，D1；这是被接受的代价，换取零新增机制）。
- 已 commit 的 `NodeCompletion` 是权威事实，升级/重派**永不覆盖或丢失**。
  fence 保证旧 attempt 的迟到结果无法覆盖新 attempt 的结论（strict-less-than
  fencing；完整快照上报 `partial=false` 整体绕过 fence 属上报语义，不改变
  commit 权威）。
- TS 侧（uc-orchestrator）在 T6 后不再持有状态权威：`.uc/tasks` 是只读 UI
  缓存，重启后由 Rust 拉取重建投影；任务级断电恢复 = Rust 侧 READY 节点
  重派 + claim loop 认领，无 wave checkpoint。

## 3. stale-drop 计数在哪里看

- 升级窗口内若仍有旧格式派发消息在途（理论上 lockstep 重启后不该有），
  worker 对**缺少 contract_version / graph envelope 字段**的派发消息执行
  reject-and-drop（ack-no-redelivery term，不进 DLQ——权威重派来自 READY
  节点，队列里的旧消息是纯垃圾），并递增 worker 侧
  **`stale_dispatch_dropped`** 计数器。
- 计数经 worker 心跳上报 → gateway 聚合（`worker_stale_dispatch_dropped`）
  → **gateway health / Dashboard** 暴露（`dashboard_service.rs`）。
- 排查口径：lockstep 重启完成后该计数应停止增长且保持不变；持续增长说明
  窗口内有旧 worker/旧 gateway 仍存活（回到第 1 节检查注册与版本）。

## 4. 回滚：旧镜像 + 重导入

- 回滚 = **全部组件换回旧镜像 + 重走 T2 的启动导入**：
  uc-grpc-server 启动时把 PG tasks（JSONB）与 `.uc/tasks` 缓存导入 graph
  表（仅当 PG 无对应 graph 时执行，`main.rs` 启动路径）。
- 已 commit 的结果在旧镜像下仍可读（graph 表向后由导入重建的路径与
  T2 交付一致）；未 commit 的 READY/RUNNING 节点回滚后按旧语义重新执行。
- 回滚后再次升级 = 重走第 1 节的 lockstep 窗口（导入是幂等的一次性路径）。

## 附：C1 reaper 窗口语义合并（行为变化记录）

T6 C1 起，**legacy dead-worker reaper 被删除**（`reassign_stale_subtasks` /
`reassign_stale_assigned_subtasks` 两函数及其窗口），由 graph-plane
`timeout_sweep` 统一接管（heartbeat monitor 每 tick 注入调用）：

| 旧语义 | 新语义 |
| --- | --- |
| dead-worker：`heartbeat_timeout` 窗口扫描 | 合并进单一 sweep 窗口 |
| stale-Assigned：300s 固定窗口扫描 | 合并进单一 sweep 窗口 |
| Assigned 未拾起需独立 reaper | `schedule_attempt` 直接以 `RUNNING` 建行，`COALESCE(heartbeat_at, started_at)` 使未拾起 attempt 同样被 sweep 覆盖 |

`transition_ok("RUNNING", "READY")` 即 fence re-arm 边沿：sweep 失败化
attempt（epoch bump）→ 节点回 READY → 重派。**运维可感知的差异只有窗口
长度**：两套窗口（heartbeat 超时 + 300s Assigned 窗口）变成一个
attempt-超时窗口，滞留节点的回收时间以 sweep 配置为准。
