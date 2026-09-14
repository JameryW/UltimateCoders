# Durable Agent Runtime 迁移评估件

> 状态：**已批准（2026-09-11）**。本文件不是实现计划，也不是工单，只回答三个问题：方案与现状的差距到底在哪、迁移有哪些必须显式做出的取舍、P0 阶段建议怎么拆。
> 决策结果：D1=状态表权威+事件审计；D2=**一刀切反转**；D3=**一刀切新契约**；D4–D7 全部闭环（D5 于 2026-09-14 裁决：Python 保留 MergeArbiter 执行 + Rust 签发 fenced barrier 授权，见 #634）；起步=wayfinder 地图+决策票。P0（T1–T7，#637–#643）已全部交付并关闭（2026-09-14），map #632 已关。
>
> 方案原文：《UltimateCoders 下一阶段：统一 Durable Agent Runtime》（2026-09-11，用户提供）。
> 现状勘察基于 commit `115caae`（main）。所有"现状"结论均附文件路径，可复核。

## 一、一句话结论

方案的方向（Rust 唯一执行权威 → Durable Graph → Ready-Node → Attempt/幂等 → Executor 解耦）与代码现状**高度吻合，且比方案预设的更近**：PG 基建、ready-node 逻辑、事件流都已在 Rust 网关存在。真正的 P0 不是"新建 Runtime"，而是**把并行存在的两套 TaskStore（TS 权威 + Rust 网关权威）收敛成一套 Graph 化的 Rust 权威，并补上 Attempt/commit-once/契约信封这些正确性缺口**。

## 二、方案的前提修正（勘察发现，与方案假设不一致处）

### 2.1 "Wave 等全部完成" 在远程路径上其实已经不存在

Rust 网关侧早就是 event-driven ready-node：每个 subtask 状态更新后会调用 `get_ready_subtasks`（Pending + 全部依赖 Completed）并立即发布（`crates/uc-grpc/src/server.rs` L1418 / L2056）。真正的 Wave barrier 只活在 **TS 本地路径**（`splitWavesByFileOverlap`，`packages/uc-orchestrator/src/orchestrator/scheduler.ts`）和 `resumeFromWave` 语义里。

> 影响：P0-3"把 Wave Scheduler 干掉"的工作量被高估——不是实现 ready-node，而是**删除 TS 的 wave 概念并把其 DAG/文件重叠逻辑吸收进 Rust 图**。验收指标 "Wave barrier = 0" 需要同时重定义 pause/resume 的恢复语义（现在按 wave 边界 checkpoint）。

### 2.2 Source of Truth 不是三份，是"两份半"

- **TS**：`.uc/tasks/*.json` + 启动时 `restore()` 重建内存 Map，且 gRPC 重连时 **TS 向 Rust 全量 push 本地任务**（`resyncAllTasksToGrpc`）——对本地路径这是事实权威（`orchestrator.ts` L319/L338）。
- **Rust 网关**：内存 DashMap `TaskStore` 为读权威 + PG write-ahead 恢复（`server.rs` L575 注释、`load_tasks_from_backend`）+ NATS snapshot 再水合。对远程 dispatch 路径是权威。
- **Python**：worker 侧无权威，只有 best-effort checkpoint（gRPC memory `key_scope="checkpoint"`），符合方案的期望。

> 影响：方案的"消灭多控制面"目标成立，但收敛动作是明确的：**先把 TS 的写路径改为"转移必须经 Rust API 并以其结果为准"，`.uc/tasks` 降级为只读 UI 缓存**，而不是先建表。

### 2.3 PG 已经在了，但形态不对

`storage` feature 下已有 `PostgresTaskBackend`：`tasks` 表把 **subtasks 整体存成一个 JSONB blob**（`crates/uc-engine/src/task_store.rs` L197），另有 `agent_events`、`scheduled_tasks`、advisory-lock 迁移（`migration_lock.rs`，#631 刚修完冷启动迁移竞态）。compose 里 postgres:16 已 provision（`UC_TASK_BACKEND=postgres`）。

> 影响：P0-2 不是"接入 PostgreSQL"，是**把 JSONB-blob-of-whole-task 规约成 graph/node/attempt/event 四张关系表**。迁移风险集中在双写与回填，基建风险基本为零。

### 2.4 幂等只有一层，且在错误的层

- Rust 发布侧：`message_id = "{task}:execute:{subtask}:{millis}"` + 内存 TTL 去重（`server.rs` L2117/L510/L533）。millis 参与键 → 重发即新键，**去重形同虚设**。
- JetStream 配了 `duplicate_window=120`，但 worker 发布 **从不设置 `Nats-Msg-Id` 头**（`python/ultimate_coders/agent/nats_worker.py`），窗口没有输入。
- Worker 侧：无 attempt 概念、无去重表；重试是进程内 `MAX_RETRIES=3` 循环，对 NATS 不可见；Core-NATS queue-group 回退路径是 at-most-once。
- 无 worker epoch/fencing：晚到的旧 attempt 结果今天**可以**覆盖新 attempt 的结论（reaper `reassign_stale_assigned_subtasks` 300s 重派 + 迟到结果，正是要防的脑裂场景）。

### 2.5 方案第 21 节"不要做"清单在现有代码里已有对应物

`DispatchMode::Local` 今天只是**跳过发布**（no-op，`server.rs` L2108），分解路径按 NATS 可用性分叉（有 NATS→Python 分解；无 NATS→Rust 本地按换行切 `decompose_task`）——正是"基础设施故障改变了 WHAT"的实例，会被 Executor 统一直接消灭，不需要额外设计。

## 三、真实缺口清单（P0 视角）

| # | 缺口 | 现状证据 | 方案对应 |
|---|------|----------|----------|
| G1 | 无 ExecutionGraph/Node/Attempt 数据模型，状态是整 Task JSONB blob | `task_store.rs` L197 | P0-2 |
| G2 | 双权威：TS 全量 push 覆盖 Rust | `orchestrator.ts` L319 | P0-1 |
| G3 | Node/Attempt 未分离；迟到结果无 fencing；node 无法 commit-once | `server.rs` L2811-2879 reaper；无 attempt 表 | P0-4 |
| G4 | 消息幂等键错误层 + worker 端零去重 + 无 `Nats-Msg-Id` | 见 2.4 | P0-4 |
| G5 | 无 Executor trait；Local 执行是 no-op；分解随 NATS 分叉 | `dispatcher.rs` L361、`task_store.rs` L566 | P0-5 |
| G6 | 取消不下沉 subtask/attempt 级（`CancelTaskRequest` 无 subtaskId） | `grpc-bridge.ts` L570 侧 proto | P0-1 附带 |
| G7 | 聚合无版本号/乐观并发；事件只有全局 AtomicU64 offset | `events.rs` L155 | P0-2 |
| G8 | TS wave 语义与 resumeFromWave 阻碍 ready-node 收敛 | `scheduler.ts`、`task-store.ts` | P0-3 |

可复用的资产（不需要重写）：PgPool/storage feature/迁移锁、`get_ready_subtasks` 及其调用点、JetStream `NatsEventStore` + EventStore trait、WatchTask 流、WorkerRegistry 能力匹配、Python WorkspaceManager/Worktree 与 MergeArbiter（P1-2 前留在 Python）、uc-types 状态枚举（可加性演进，注意 lib.rs 显式 re-export）。

## 四、必须显式拍板的取舍（未决问题）

**D1 · Graph 状态的存储形态 — ✅ 已决：状态表权威 + 事件审计**
`execution_graphs/graph_nodes/task_attempts` 行（带 `version` 列 CAS）为唯一权威；`execution_events` 仅作审计/回填；JetStream 继续承担实时流。沿用现有 `load_tasks_from_backend` 骨架。

**D2 · 双权威收口过渡策略 — ✅ 已决：一刀切反转**
一个版本内 TS 停止自行转移状态，删除 `resyncAllTasksToGrpc` push，`.uc/tasks` 只读化。**连带约束（必须随票交付）**：
1. 需要一个**显式的启动回填步骤**：反转上线后，Rust 从 PG 行表读权威，存量 `.uc/tasks/*.json` 只在"PG 无对应 graph"时一次性导入并标记，不再双向。升级窗口 in-flight 任务处置已由 D7/#636 定案：自动续跑（未 commit attempt 超时→fence→重派），不排空、不丢弃。
2. 回滚预案：反转后 TS 不再持有权威，回滚 = 退回旧版本镜像并重新 import；此风险由用户显式接受。

**D3 · worker 契约切换 — ✅ 已决：一刀切新契约（无兼容窗口）**
信封直接换成 `graph_id/node_id/attempt_id/idempotency_key/worker_epoch + contract_version`，不双写 task_id/subtask_id。**连带约束**：
1. gateway 与 worker 必须 lockstep 升级——需要一条**握手校验**：worker 注册/心跳时上报 contract_version，gateway 拒绝不匹配版本的 subtask 派发并明确报错（防止混跑时静默拒单/假死）。
2. `contract_version` 字段仍保留（握手用），但只接受当前版本。
3. 跨主机部署文档（`UC_SCALE_HOSTS`/remote worker 章节）需同步"先升 worker 还是先升 gateway"的硬性顺序说明。

**D4 · NATS 传输收敛 — ✅ 已决（#633，2026-09-11）**
① 派发 JetStream-only 硬依赖：删除 worker Core 静默降级（`nats_worker.py` L486–503），JS 不可用→拒绝注册+周期重试+健康/Dashboard 可见，UC_SUBTASKS 流创建归 gateway 侧供给；② LocalExecutor fallback 按节点 `effect_class` 白名单（仅 `local_safe` 节点可就地执行），Coding 节点保持 READY 排队+告警，不重分解、不改协议（WHAT 冻结，只降 HOW/WHERE）；③ P0 只收敛派发面：事件/控制通道（update/event/heartbeat）维持 core，node/attempt 级 cancel 语义归 T7。完整决议见 #633 resolution comment。

**D5 · Commit barrier 的归属（P1-2 的前置决策，现在只需要记录）**
MergeArbiter 在 Python。方案要求 merge 是 single-writer 事务。选项：Rust 重写 arbiter vs Python 保留执行、Rust 只发放带 fencing 的 barrier 授权（`merge_idempotency_key = graph_id+node_id+commit_sha` 由 Rust 签发）。建议后者，P0 不动。

**D6 · resume 语义（#635）— ✅ 已决（2026-09-11）**
① node 态重算：pause=图级停派闸（Rust 现行为即如此，`task_store.rs` L91–L125），resume=从 NodeCompletion 重推 READY，不存图快照；TS `resumeFromWave` + `.uc/checkpoints` 随 T6 删除，checkpoint 退化为 execution_events 审计流。② RUNNING attempt 软暂停 + `UC_PAUSE_GRACE_SECS`（默认 120s）超时升级 node 级 cancel（机制归 T7）。③ grace-cancel 不终态化 node：fence attempt、node 回 READY——"cancel-attempt-keep-node"进 T7 验收。

**D7 · 升级窗口 in-flight 处置（#636）— ✅ 已决（2026-09-11）**
① 自动续跑：一次性导入后已 commit node 保留，窗口内 RUNNING attempt 超时→fence→node 回 READY→新信封重派（D1/D6 模型本身，零新机制），未 commit 半程工作从零重跑为已接受代价；无排空门禁/丢弃/人工闸门。② 滞留旧信封消息：新 worker term 丢弃 + `stale_dispatch_dropped` 计数告警，不建 DLQ。③ T6 发布说明四要点（升级顺序、重跑语义、计数位置、回滚路径）见 #636 comment。

**遗留风险（本评估件无法在代码里解决）**
- 验收指标 Useful Work Ratio / Coordination Ratio / Activation Inflation 目前**没有任何采集点**；若采纳，需要在 P0-2 的 event 表里预留 `cost/tokens/duration` 字段，否则 P2 的 Optimizer 没有数据可优化。
- TS 侧 review 链路（`reviewSubtask` 已定义但未接入 wave 循环）在收敛时会被暴露为死代码，需要决定去留。

## 五、P0 工单草案（vertical slices，含阻塞边）

按 issue-flow：D1–D4 已定案（D4 via #633）；剩余开放决策（见本节末）进 wayfinder 地图，闭环后建实现票。拟 7 张：

| 票 | What to build | Blocked by |
|----|---------------|-----------|
| T1 | 信封契约定义：proto/NATS payload 换成 graph/node/attempt/idempotency/epoch 字段 + `contract_version` 注册/心跳握手（D3：无兼容窗口，混跑明确报错） | 决策 D4 |
| T2 | PG 迁移：`execution_graphs/graph_nodes/task_attempts` 三表（状态表权威，D1）+ 从 tasks JSONB 的一次性回填 + `.uc/tasks` 首次导入 | T1 |
| T3 | Rust 图状态机：node 状态集（CREATED…SKIPPED）+ version/CAS + `NodeCompletion` commit-once（`INSERT … ON CONFLICT DO NOTHING`）+ attempt 心跳/超时判定；替换 reaper 的 reassign 逻辑 | T2 |
| T4 | 正确性闭环：`Nats-Msg-Id` 设置、修正 message_id 键、worker 端 attempt 级去重（结果存在→ack no-op）、迟到结果按 epoch/attempt 拒绝 | T3 |
| T5 | Executor trait 统一：Local/Nats(JetStream-only)/Sandbox/Remote 实现 + 消灭 `decompose_task` 本地分叉与 no-op DispatchMode::Local；NATS 故障只换 HOW | T3, 决策 D4 |
| T6 | 权威反转（D2 一刀切）：TS 状态转移全部改为经 Rust API 生效、删 `resyncAllTasksToGrpc`、删 TS wave 循环与 wave 快照（ready-node 由 Rust 独任）、按 D6 实现软暂停/宽限硬停/node 态重算 resume、发布说明按 D7 四要点（自动续跑+回滚步骤） | T3, T4 |
| T7 | 取消下沉：node/attempt 级 cancel RPC + NATS 控制信号 + worker 响应；含 D6 的 cancel-attempt-keep-node（fence attempt→node 回 READY）；TS cascadeCancel 删除 | T6 |

P1（Scope、Commit Barrier、Context Compiler、Sandbox 白名单、affinity placement）在 P0 验收后另开地图，不在本件展开。**P1 地图已建（2026-09-14，#644）**：决策票 D8=#645（ExecutionScope 模型）、D9=#646（barrier grant wire/storage，D5 后续）、D10=#647（Context Compiler 归属与契约）、D11=#648（sandbox env allowlist）、D12=#649（PlacementScore 维度与计算位置）；五票全闭后建 T8–T12 实现票。

**wayfinder 地图开放决策**：无。D4（#633）、D6（#635）、D7（#636）、D5（#634，2026-09-14 裁决：hybrid——Python 保留 MergeArbiter 执行，Rust 签发 fenced merge-barrier 授权，`merge_idempotency_key` 走 envelope 同族确定性派生）已全部闭环。

**T1–T7 已建票（2026-09-11）**：T1=#637, T2=#638, T3=#639, T4=#640, T5=#641, T6=#642, T7=#643；原生 blockedBy 边：638←637，639←638，640←639，641←639+633(D4)，642←640，643←642。**当前唯一可开工票：#637（T1）**。

## 六、执行状态

评估件已批准（2026-09-11）。**P0 已全部交付并关闭（2026-09-14）**：T1–T7（#637–#643）逐票关闭并归档（journal session 1–8），D4–D7 四张决策票全闭环，map #632 已关。遗留：三笔 `#[ignore]` PG e2e 实跑（T4 graph_store_integration / T6 pause_grace_diamond / T7 granular_cancel_e2e，`-- --ignored`）待 Docker 恢复补跑。P1（Scope、Commit Barrier、Context Compiler、Sandbox 白名单、affinity placement）按上节另开地图；D5 裁决即 P1-2 的输入。
