# T6: 权威一刀切反转 + TS wave 机器拆除 + D6 暂停语义 + D7 发布说明

- Issue: #642
- Blocked by: T3（已落地）, T4（已落地）, D6 #635（已决）, D7 #636（已决）
- Design refs: D2 一刀切反转（含连带约束）, D6 ①②③, D7 四要点, §3 G2/G8
- 日期: 2026-09-14

## Goal

一个版本内完成权威反转：TS 停止自行转移状态（全部经 Rust API 并以其结果为准），
`.uc/tasks` 降级为 UI 缓存；TS wave 机器整体拆除（ready-node 由 Rust 独任）；
Rust legacy reaper（reassign 两函数）被 graph-plane `timeout_sweep` 取代并接进
heartbeat monitor；D6 软暂停 + `UC_PAUSE_GRACE_SECS` 宽限硬停 + node 态重算
resume 落地；D7 四要点发布说明交付；TS review 死代码处置。

## Scope（C1–C7 切片，按此顺序提交，`Tracker: #642`）

### C1 — Rust reaper 换血（graph-plane sweep 接管）
- `spawn_heartbeat_monitor`（server.rs L3212-3304）接入 `timeout_sweep`：
  monitor 增 `Option<Arc<GraphStore>>` 注入（storage feature 门禁），每 tick 调
  `sweep(heartbeat_timeout, DEFAULT_MAX_ATTEMPTS)`。
- `timeout_sweep`（graph_store.rs L1478）返回值从 `Vec<String>` 扩展为带
  `(graph_id, node_id, attempt_id, FailOutcome)` 的结构（monitor 桥接需要 outcome；
  attempt_id 已含 graph:node:retry 信息但结构化返回更干净）。
- 桥接语义：`RearmedToReady` → legacy subtask 回 `Pending`（清
  `assigned_subtask_times`）+ WatchTask 事件；`NodeFailed` → legacy `Failed`；
  随后对受影响 task（graph_id == task_id 身份映射）调 `dispatch_ready_subtasks` 重派。
- 删 `reassign_stale_subtasks`（server.rs ~L1749）+ `reassign_stale_assigned_subtasks`
  （~L1899）+ 四个单测（L6028-6120）+ `graph_fail_verbs_fire_at_reaper_waypoints`
  （L7794，重写为 sweep 桥接路径断言）。`mark_stale_workers` + registry 注销保留
  （registry 卫生与 subtask 重派解耦）。注意 monitor 内 tokio::Mutex 不可重入的
  drop 纪律（L3272-3280 注释）。
- 语义合并（记录进 C6 发布说明）：legacy dead-worker（heartbeat_timeout 窗口）与
  stale-Assigned（300s 窗口）合并为单一 sweep 窗口——`schedule_attempt` 直接以
  'RUNNING' 建行（graph_store.rs L1203），Assigned-未拾起同样被
  `COALESCE(heartbeat_at, started_at)` 扫描覆盖；`transition_ok("RUNNING","READY")`
  即 fence re-arm edge（L2035）。

### C2 — T4 遗留 serde 回落删除（Rust + Python 同步）
- Rust：`NatsSubtaskExecute.task_id/subtask_id` 字段删除（L243-249，注释已预告
  "Fields stay for T6 cleanup"）；`subtask_execute_payload` 两处 `String::new()`
  删除（L352-353）；相关单测修订（round-trip L7164-7204、legacy 反序列化
  L7256 删除、with_envelope L7247 保留）。
- Python：`_parse_subtask_message` 的 legacy 字段回落删除（graph envelope 成为
  唯一解析路径；serde default 字段不删——wire 上旧消息不再被读取即可）。

### C3 — TS 权威反转 + `.uc/tasks` 只读化
- 删 `resyncAllTasksToGrpc`（orchestrator.ts L2103-2112）+ 连接上升沿调用（L320，
  兼任首次连接 bootstrap 的双重身份）。
- bootstrap 替代：首次连接改为 TS 从 Rust 拉——`grpc-bridge.listTasks` 封装已存在
  （未接线），拉取结果作为只读投影回填本地 Map；TS 不再 push。
- 本地路径状态转移改为"经 Rust API 生效并以其结果为准"：本地突变（start/complete/
  fail）先经 update_task RPC，以响应回填本地镜像；RPC 失败则放弃本地转移（不落盘、
  不缓存）。
- `.uc/tasks` 只读化为 UI 缓存：TS 仍写缓存文件作投影（UI 离线可见），但 restore()
  不再作为权威来源（权威 = Rust 拉取结果）；删除缓存文件后 UI 可由 Rust 重建。
  Rust 侧启动导入已由 T2 交付（uc-grpc-server main.rs L347：仅 PG 无对应 graph 时
  导入），C3 不新写 Rust 回填。
- TS pauseTask/resumeTask 本地状态手术删除（failed→pending 重置 + 重切波，
  L1170-1239），只透传 RPC 并以响应为准。

### C4 — TS wave 拆除 + D6 暂停语义
- 删 `splitWavesByFileOverlap`（scheduler.ts L217-267）/ `executeWaves`（orchestrator.ts
  L688-859）/ `executeWave`（L861-999）/ `resumeFromWave`（scheduler.ts L110、
  task-store.ts L22）/ `saveCheckpoint`/`loadRecoverable`/checkpoint 提取器
  （task-store.ts L156-172；`.uc/checkpoints` 整目录退役，checkpoint-spec 记录）。
  两个提交路径的切波点（L565/588、L639/661）改直提。
- 替代执行环：任务 upsert 全图 Pending 后，TS 本地执行体改为"从 Rust 状态认领"——
  复用 remote 路径的 getTask 轮询/WatchTask 事件感知 READY 节点，本地认领执行，
  完成经 Rust API 上报，Rust 侧现有 publish/dispatch 路径推进后续 ready-node
  （含 NATS 远端派发）。并发上限沿用执行体级设置。
- FileIntentTracker/CircuitBreaker 处置：CircuitBreaker 保留在本地执行体级（重试
  熔断）；FileIntentTracker 退役，其文件重叠感知职责移交 C5 conflict_risk（去向记录
  进票面）。
- D6 落地：pause = 现有 RPC 停派闸（`get_ready_subtasks` 要求 InProgress，server.rs
  L1823）已成立；补 `resume_task` RPC 的重派（当前 L4387-4457 不调
  `publish_ready_subtasks`——resume 后 node 态重算 + 立即重派）；软暂停 grace timer：
  pause 时启动 `UC_PAUSE_GRACE_SECS`（默认 120s）计时，超时对仍 RUNNING 的 attempt
  走 `fail_attempt(reason="pause_grace_expired")`（fence attempt + node 回 READY =
  权威侧 cancel-attempt-keep-node；worker 侧协作 cancel 信号归 T7）；resume 取消
  计时器。
- 菱形图 TS-free 端到端验收测试（Rust 集成测试，grace 调小）：暂停→单分支 commit→
  宽限硬停→resume 重派。

### C5 — conflict_risk 分级（替代文件重叠硬禁并行）
- 分解期 TS 计算：same file/class/symbol 重叠 → 0.4/0.8 阈值分级（low/medium/high）。
- 不进 graph schema（NodeRow/graph_nodes 无文件列，不过度设计）；随 subtask 定义
  透传，为 P1 affinity 调度留接口。
- 本地认领批量选点时消费：high 不与本批其他节点并行、medium 限同批 1 个、low 自由。

### C6 — D7 发布说明
- docs/ 发布说明四要点：①升级顺序 = gateway 与 worker 同一 lockstep 窗口（contract
  握手拒绝旧 worker）；②未 commit 的 in-flight 从零重跑、已 commit 永不丢失；
  ③stale-drop 计数位置（gateway health / Dashboard）；④回滚 = 旧镜像 + 重导入。
- 附带记录 C1 的 reaper 窗口语义合并。

### C7 — TS review 死代码处置
- 删 `reviewSubtask`/`reviewSubtaskLocal`/`reviewSubtaskRemote`（L1860-2000）、
  `parseReviewOutput`（L164）、`ReviewResult` 类型、从未 emit 的 `subtask_reviewing`
  事件、"Review rejected" 判定（L1639）、`WORKER_PROMPT`/`SUPERVISOR_PROMPT` 死配置
  （L2256-2285）、parse-review 测试。去向记录进票面（评审语义未来由 Rust 侧
  ready-node 管线重立，不在本迁移范围）。

## Test seams

- 崩溃注入恢复测试（graph_store 集成层）：重启后无重复执行已 commit node、
  无丢失 READY 节点。
- 菱形图 TS-free 端到端（Rust 集成测试）：经 Rust API 暂停→单分支 commit→宽限
  硬停→resume 重派。
- pytest：`_parse_subtask_message` 对 legacy 字段不再回落（graph envelope 唯一路径）。
- TS 测试：scheduler.test.ts wave 用例删除/重写为 ready-node 认领语义；task-store
  checkpoint 用例删除；watch-task-recovery / task-bridge / reverse-cascade 存活。
- 无头 grep：`resumeFromWave` / `checkpoints` / `splitWavesByFileOverlap` /
  `resyncAllTasksToGrpc` 全仓零匹配；`.uc/tasks` 缓存重建证据（删文件→Rust 重建）。

## 门禁

- fmt / clippy `-D warnings`：uc-types、uc-engine（默认 + `--no-default-features`）、
  uc-grpc `--all-features`、uc-grpc-server。
- Rust 测试基线（T5 终值，只增不减）：437+5 / 379+5 / 192+8 / 36 / 35。
- pytest 全量：977 passed + 4 skipped 基线（bypass + 浅 basetemp 跑法）。
- TS 测试基线：开工前先记录 pre-change 通过数，只增不减（packages/uc-orchestrator）。

## Out of scope

- worker 侧协作 cancel 信号与 TS cascadeCancel 删除（T7 #643；D6 原文：grace 升级的
  node 级 cancel 机制归 T7，pause 是其首个调用方——本票只交付权威侧 fence + seam）。
- D5 commit barrier（P1）；affinity 调度（P1）；MergeArbiter 迁移。
- Rust 侧启动回填新代码（T2 已交付 `.uc/tasks` 导入，main.rs L347）。
