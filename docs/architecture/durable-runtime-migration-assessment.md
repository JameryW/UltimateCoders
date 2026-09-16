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

P1（Scope、Commit Barrier、Context Compiler、Sandbox 白名单、affinity placement）在 P0 验收后另开地图，不在本件展开。**P1 地图已建（2026-09-14，#644）**：决策票 D8=#645（Scope=project_id 形式化+派发硬过滤+worker projects 注册）、D9=#646（gRPC IssueMergeGrant/ReportMergeOutcome + merge_grants 表 + 确定性 key）、D10=#647（网关组装 envelope context_block，加性字段，worker 优先消费）、D11=#648（deny-by-default env allowlist，单点 `\_execute_subprocess` 强制）、D12=#649（网关打分 + per-worker subject 定向 + shared overflow）——五票当日全裁。**T8–T12 实现票已建（2026-09-14）**：T8=#650、T9=#651、T10=#652、T11=#653、T12=#654（654←650 原生边）；地图 #644 保持开放至 P1 验收。

**wayfinder 地图开放决策**：无。D4（#633）、D6（#635）、D7（#636）、D5（#634，2026-09-14 裁决：hybrid——Python 保留 MergeArbiter 执行，Rust 签发 fenced merge-barrier 授权，`merge_idempotency_key` 走 envelope 同族确定性派生）已全部闭环。

**T1–T7 已建票（2026-09-11）**：T1=#637, T2=#638, T3=#639, T4=#640, T5=#641, T6=#642, T7=#643；原生 blockedBy 边：638←637，639←638，640←639，641←639+633(D4)，642←640，643←642。**当前唯一可开工票：#637（T1）**。

## 六、执行状态

评估件已批准（2026-09-11）。**P0 已全部交付并关闭（2026-09-14）**：T1–T7（#637–#643）逐票关闭并归档（journal session 1–8），D4–D7 四张决策票全闭环，map #632 已关。遗留：三笔 `#[ignore]` PG e2e 实跑（T4 graph_store_integration / T6 pause_grace_diamond / T7 granular_cancel_e2e，`-- --ignored`）待 Docker 恢复补跑。P1（Scope、Commit Barrier、Context Compiler、Sandbox 白名单、affinity placement）按上节另开地图；D5 裁决即 P1-2 的输入。

**P1 已验收并关闭（2026-09-15）**：T8–T12（#650–#654）逐票关闭并归档，D8–D12 五张决策票全闭环，map #644 已关。**P0 那条遗留已清零**——三笔 `#[ignore]` PG e2e 已在 CI 实跑全绿（`graph_store_integration` **17 passed** / `granular_cancel_e2e` **2 passed** / `pause_grace_diamond` **1 passed**，见 `storage integration tests` job）。另修 T13（#655）：source-B 导入把阻塞的 `Pending` 节点发布成 `READY`，三条图投影路径现共用一份依赖感知规则。**本仓不再欠任何「只能由 CI 验」的分支。**

**P2 地图已建（2026-09-15，#656）**：范围 = Execution Optimizer / Blackboard review / market scheduling（承 #644 的 out-of-scope 行）。开图前勘察核了 §四 两条「遗留风险」的实际形态，与原文措辞有出入，**本节据此更正 §90 的口径**：

- **§90（指标采集点）**：原文称「需要在 P0-2 的 event 表里预留 `cost/tokens/duration` 字段」——**该前置已由 T2 满足**：`execution_events` 早就有 `cost` / `tokens` / `duration_ms`（`graph_store.rs:806-808`）。实际缺的是**两跳**：①**上报契约**——worker 上报的 `SubtaskResult` 没有 usage 字段（`python/ultimate_coders/agent/types.py:67-80`）；②**持久化写入点**——唯一 INSERT 不写这三列（`graph_store.rs:2290`），且全仓无读取（Rust 仅出现在 schema 定义，Python 完全不引用 `execution_events`）⇒ 今日是彻底死列。而适配器**两侧均已采集** token/cost（Rust `AgentOutput.token_usage`，extractors `sandbox/agents/claude_code.rs:221`、`grok.rs:280`；Python `sandbox.py:354` + `_grok_usage:1244` 连 `total_cost_usd` 一起解析），**duration 还可直接由 `task_attempts` 时间戳推导**（`graph_store.rs:777-779`）⇒ 真正缺的只有 cost/tokens，且缺在契约与写入点，**不在采集、也不在 schema**。
- **§91（review 死代码）**：**已由 T6 处置**——TS review 流水线（`reviewSubtask` 等 + `parseReviewOutput` + 两个 prompt）已删除，原处注释同时写明前向口径「review semantics will be re-established by the Rust ready-node pipeline (out of scope here)」（`packages/uc-orchestrator/src/orchestrator/orchestrator.ts:1783-1790`），`SubtaskResult.review` 字段刻意保留给未来产出方。该项即 P2 的 Blackboard review，来源已锚定。

决策票：**D13** metrics-collection-points **#657**（gates P2-1 Execution Optimizer）、**D14** review-plane-ownership **#658**（gates P2-2 Blackboard review）。⚠️ **market scheduling 的范围无法在仓内坐实**（全仓无任何 `market` 标识符，也没有独立 scheduler 模块），需外部「方案第 21 节」原文才能转成决策票；本图未臆造其内容。

**P2-1 第一片已交付（2026-09-15）**：T14（#659，承 D13 #657）= 指标写入点落地。裁决口径是先做**零契约变更的那一半**：`duration_ms` 由 `commit_once` 的成功路径写进终态事件，`cost`/`tokens` 保持 NULL 待 T15 补上报契约。写入点选在 commit-once 的**赢家分支**⇒ 结构上恰好一次（fenced 早返与 lost-insert 两条分支都到不了它，重试不可能重复计数；被 fence 的迟到结果落成 `late_result` 且 `duration_ms` 为 NULL）。三条诚实规则同时写进代码注释与单测：无 `started_at` 时写 NULL 而**不写 0**（写 0 是「编造时长」）；两个时间戳反向（时钟偏斜）也写 NULL 而**不取绝对值**（不把偏斜「编」成一个看起来合理的数）；`NOW()` 取**数据库时钟**且在**同一把行锁**下读（应用/PG 偏斜不会被伪装成合理时长）。终态事件 payload 带 `usage_reported: false` ⇒ 下游能区分「零」与「缺失」，D13「缺失留 NULL **不得当 0**」在本片以最保守形式生效。本片之所以零契约变更，是因为两个时间戳**本就在手**：`started_at` 由 `schedule_attempt` 盖、`finished_at` 由 `commit_once` 同事务内 `UPDATE`。⚠️ 一处诚实记录：D13/票面写的函数名是 `node_duration_ms`，落地为 `attempt_duration_ms`——它量的是**一次 attempt**（一个 node 可以有多次），行为与票面逐条一致，改名理由已写进票面与 journal。

**T14 暴露并处置了一条测试确定性缺陷（2026-09-15，#664）**：`granular_cancel_e2e` 原本靠 `settle()`（30×`yield_now()` + 50ms sleep）等 `persist_task` 的图 fan-out，而该 fan-out 是 **fire-and-forget 且效果不可观测**——`upsert_task_shadow` 无条件覆盖（`ON CONFLICT … DO UPDATE SET state = EXCLUDED.state`，既无 `transition_ok` 守卫也无 version CAS），且重贴的正是图里已有的状态，**没有可轮询、可 await 的落点** ⇒ 慢 runner 上一份迟到的 `InProgress` 快照会盖掉刚裁决的 `READY`（CI 两次同点失败，`left: Some("RUNNING") right: Some("READY")`）。**判据是「不是我的回归」，而不是「不是我改的文件」**：同一二进制哈希（代码未动）在 `5bae604` 跑 0.89s、在 `437265d` 跑 4.17s；同一 job 里我未触碰的 `context_block_integration` 也从 0.56s→3.19s；而**共用同一个数据库**的 `graph_store_integration`（2.51→2.52s）与 `merge_grant_integration`（0.62→0.61s）纹丝不动 ⇒ 变慢的是那两个各自 `DROP/CREATE DATABASE` 的二进制，即 **runner 的 DDL 速度变了**。修法是测试侧**去掉对不可观测通道的依赖**（显式投影图状态，fan-out 本身仍由 uc-grpc 单测覆盖），不动产品语义。

**D15 已开（2026-09-15，#665）**：上述缺陷的**产品面**问题另立决策票——图镜像（best-effort 派生物）**能否回退**图平面（权威）已裁决的 node state。列了四个选项（A 原样保留 / B seed-only / C `transition_ok` 守卫 / D 时间戳守卫），并记明 `RUNNING → READY` 本身是**合法边**，故单靠转移表守卫不足。**未裁，故不开 T 票**；本图由此新增一项开放决策。

**T14 + #664 的 CI 验收（2026-09-15，`0c2604d`）**：Rust CI **8/8 全绿**。`storage integration tests` job 内：`graph_store_integration` **18 passed / 2.22s**（含新增的 `graph_t14_commit_binds_duration_once_and_never_zero_fills_cost ... ok`，即本片唯一的「只能由 CI 验」分支已实跑通过）、`granular_cancel_e2e` **2 passed / 0.45s**（#664 修复前同二进制 4.17s / 1 failed——修完不但确定，且**不再等待**，故 0.45s 同时是「时序敏感已被移除」的证据）、`merge_grant_integration` **4 passed / 0.61s**、`pause_grace_diamond` **1 passed / 0.86s**。`test (no storage feature)` 亦绿 ⇒ 门控正确性由 CI 独立复核（本地已按数字验过：默认 feature lib 441→443 为 +2，`--no-default-features` lib 383→383 为 +0）。

**P2-1 第二片已交付（2026-09-15）**：T15（#660，承 D13 #657）= 上报契约 + `cost`/`tokens` 落列。D13 那两列**至此打通**：契约加性携带 usage → 网关在 commit-once 赢家分支与 `duration_ms` **同事务同条**写入 → 恰好一次仍是结构性的（fenced 与 lost-insert 两条早退都到不了那一支，重试不可能重复计数）。落地形状有三点值得记：① **`uc_types::SubtaskUsage` 同时承运 domain 与 wire**（`SubtaskResult.usage` 与 `NatsSubtaskUpdate.usage` 共用一个类型）⇒ 键名与字段不可能分叉，这比「两侧各自定义再祈祷一致」强；② 四字段全 `Option` + `serde(default, skip_serializing_if)` ⇒ 不设 usage 的发布者**序列化结果字节不变**，故 `contract_version` 未动；③ 两条 D13 硬要求写成**单测**而非注释——`is_empty()`「没有数字的块不是测量」（**只带来源的块**同样不算），`total_tokens()`「两侧都缺 ⇒ NULL，不写 0」（单侧有值仍是真实的和）。终态 payload 用 `usage_reported` 显式声明本事件是否携带测量、`usage_source` 在已知时点名适配器（**缺则 null，不推断**），D13「缺失留 NULL 不得当 0」由此在 payload 层也可读。

**T15 的四处诚实记录**：① **真正的入口是 `NatsSubtaskUpdate` 而非 `SubtaskResult`**——后者是网关自己构造的 domain 类型（`nats_subtask_to_domain`）、**不从 wire 反序列化**，而 wire 上 `result` 只是一个 summary 字符串；只在 domain 上加字段，worker 那份用量**永远到不了网关**。② 票面第 4 条「Rust 本地执行路径也落列」**按偏差交付**：该路径今天**不存在**（`parse_output` / `create_adapter` / `available_agents` 全仓无生产调用者，`SandboxExecutor::available() == false` 且 `execute` 返回 `Unsupported(...)`，`LocalNodeHandler` 只有测试替身）——这正是 D13 那句「`token_usage` 在 sandbox 模块之外无消费方」的成因；故交付为转换 helper + 单测，**不声称本地路径已落列**。③ 票面之外的**第三跳**顺手补上：`Task.to_dict` / `from_dict`（checkpoint 往返，**不是** NATS 报文）原先不含 usage，worker 重启后重发全量快照会让同一子任务的 usage 静默消失——与 D13 属同一类缺陷。④ 原判「通用 JSON 解析路径没有 adapter 身份 ⇒ `source` 只能空着」被**推翻**：三个解析点都在适配器方法内，`self.name()` 可用，故三处全填（`grok-build` / `claude-code`），且未编造来源。

**T15 的门禁（2026-09-15，`dd3d2b3`，本地实跑；CI 数字待 CI 回报后另记）**：`fmt --check` / `clippy --workspace -D warnings` / `check --workspace --all-targets --all-features` 三项 clean；Rust 基线只增不减——uc-types **43→47**（+4 新单测）、uc-grpc `--all-features` **237**、uc-grpc-server **36**，其余（default 443+5、nodefault 383+5、uc-grpc default lib 209 / 8）**逐项不变**；pytest **1118 passed / 10 skipped / 0 failed**（沙箱内逐文件跑会报 169 个 errors，**全部是沙箱产物**——error 集中的 11 个文件在非隔离上下文复跑后逐条转成 pass，41+38+22+13+10+7+5+2+1+24+6 恰好 169）。⚠️ **PG 实跑欠账**：新增的 `graph_t15_commit_binds_reported_usage_and_keeps_unreported_null` **未在真实 PG 上执行**（本地 live PG 通路在 `wsl.exe` 被程序黑名单拦住前实质不可用），仅做**编译验证 + 确认被收集**，执行交 CI 的 `storage integration tests` job ⇒ 不声称已通过。另记一条**流程缺陷**：调用点普查第一次漏了 uc-grpc 的 `tests/`——那 5 处 `.commit_once(` 所在文件顶部是 `#![cfg(feature = "storage")]`，而 uc-grpc `default = []` ⇒ 默认特征下被编译成**空文件**，`cargo check --workspace --all-targets` 完全看不见，只有 `--all-features` 才报 E0061。**已知限制两条**：多步 workflow 只透传最后一步的 usage（少报不是错报；聚合会让数字与 `source` 同时不可归因，逐步用量应落在逐步事件上）；worker→worker 的 `subtask_completed` 事件不带 usage（网关落列不受影响，本地编排器那份副本为空）。

**P2-2 已通路（2026-09-16，T16 #661，承 D14 #658）**：review 作为图上节点从裁决落成通路，三件事各司其职。① **`graph_nodes.type` 从「有列、有 DEFAULT、无写者」变成有写者**：唯一 INSERT 点（`graph_store.rs:1030/1038`，shadow 与非 shadow 两个变体）原先列清单里根本没有 `type`，于是每行恒为 `'subtask'`；现由 `node_type_for(required_capabilities)` 推导写入，shadow 侧另加 `type = EXCLUDED.type`。标签**由能力推导而非声明**，是为了让「自称 review 节点」与「必须要求把节点路由给 reviewer 的那项能力」无法脱钩——节点没法一边自称 review、一边不要求那条能把自己送去审的能力。② **独立性走能力门 fail-closed**：`review` 从**每一个** worker 的默认能力表里摘掉，改为 `UC_CAP_REVIEW` 显式 opt-in ⇒ 未 opt-in 的产出者根本过不了 review 节点的 `dispatch_gate`（`worker_service.rs:262`），该节点停在 `PENDING`。③ **结论跨四语言送达 TUI**：`SubtaskReview`（`uc-types`）→ `NatsSubtaskUpdate.review`（wire，与 T15 的 usage 同一**加性**纪律，故 `contract_version` 未动）→ proto `optional string review_json = 16` → `conversions.rs` 双向 + `dashboard_service` 快照 → Python 侧构造与 checkpoint 往返 → TS `grpc-bridge` 解析为 `SubtaskDef.review`，**UI 无需改动**即可显示。裁决走 JSON 字符串而非嵌套 message，是为了让消费者能把**垃圾判成「无裁决」**，而不是半填充出一条记录。**「缺席不是否决」贯穿每一跳**（Rust `Option` / proto `optional` / Python 容错 `from_dict` / TS `parseReviewJson`）：拿不到裁决就是**没有裁决行**，绝不写 `approved: false`——与 D13 对 usage 的口径同源。

**T16 侦察推翻了票面与 D14 的三处陈述**（先纠正记录、再动代码）：① **`SubtaskResult.review` 并非「已刻意保留」**——只有 TS 侧 `SubtaskDef.review`（`orchestrator.ts:106-110`）在 T6（#642）之后存活，Rust（`uc-types/src/agent.rs:210-232`）与 Python（`types.py:153-174`）两侧**都没有**任何 review 字段。D14 那句对 TS 成立、对 Rust/Python 不成立，S4 因此从「填充一个已有字段」变成「造一条跨四语言的通路」。② **`graph_nodes.type` 的「零迁移」成立、「零工作量」不成立**——列与 DEFAULT 早就在，缺的是**写者**。③ **`worker_epoch` 不是 worker 身份**，而是 per-attempt 单调栅栏计数器（`SELECT COALESCE(MAX(worker_epoch),0)+1`，`graph_store.rs:1411`），且 Python 侧恒发空串 ⇒ 票面验收「同一 epoch 不得自审」**字面上不可实现**，按语义实现为「产出被审结果的 worker 不得认领它的 review」。

**T16 的独立性有一个机制性缺口，如实记账**：票面希望用「能力 + affinity」表达「review 必须由产出者之外的人执行」，但 `placement.rs:7` 明写 affinity 是 *preference, never a gate*，`dispatch_gate` 只按 capability + scope 过滤且**没有排除参数**，而票面同时禁止给 `dispatch_gate` 加排除原语（要求先回开 D14）。故落法只能是能力门 fail-closed，其**残余缺口**是：一个**显式同时 opt-in 两种角色**的 worker 仍可自审。这个洞不隐瞒，也不靠顺手加机制去堵——加机制属于改 D14，得走决策票。

**T16 的门禁与两处诚实记录（2026-09-16，`90f756d` + `f5ef707`；`f5ef707` 上 Rust CI 8/8 绿）**：`cargo fmt` 与 `uc-types clippy --all-targets`、`uc-engine clippy --lib`、`cargo check -p uc-engine --all-targets` 均 clean；Rust 基线只增不减——uc-engine lib **443→446**（+3）、uc-types **47**、uc-grpc `--all-features` **237→238**（+1 golden）、pytest **1120→1123**、ruff clean。新增的 Rust golden 测试专门钉住 `review_json` 的**跨语言 key 名**（`approved` / `issues` / `suggestions`），因为改名会让 TUI 裁决行**静默变空**却不打红任何 Rust 测试；并断言缺 `approved` 的 JSON **必须解析失败**。① **TS 那一跳本地可验证（本段已更正）**：初版记录写的是「`bun test` 在本机稳定段错误（Bun 1.3.14，`Segmentation fault at address 0x5`），故该跳只能由 CI 验证」——**这是错的**。真因是我把 `parseReviewJson` 写成**类体内的 `function` 声明**（TS 的类方法不允许 `function` 关键字），Bun 1.3.14 撞上这个语法错误后**直接崩溃**（SIGTRAP / exit 133），并在崩溃信息里打印 *"This indicates a bug in Bun, not your code"*；我信了这句话，没有去读紧邻其上的那几行 `Expected ";" but found "parseReviewJson"`（`grpc-bridge.ts:1054:11`，本地与 CI 一字不差）。改成 `private` 后本地 `bun test` **156 passed / 0 failed / 16 files**（与既有基线一致），CI 的 `bun test (uc-orchestrator)` 亦由红转绿（修复 `04372b8`）。② **一次自我更正**：S1–S3 交付后我曾写下「本机无 protoc ⇒ uc-grpc / `--all-features` 不可编译 ⇒ S4 只能押后到 CI」——**这是错的**。`crates/uc-grpc/build.rs` 在 `PROTOC` 未设时会回落到 `protoc-bin-vendored`，`cargo check -p uc-grpc` 实测 50.14s 编过，S4 本就可达。错因是拿 `which protoc`（一个 PATH 事实）当成了构建事实；更正已落到 follow-up 提交信息、#661 评论与长期记忆三处，不只留在对话里。另两条**归因而非豁免**的红：`cargo clippy -p uc-engine --all-targets` 在 `graph_store_integration` 报 `can't find crate`（把文件取出、`git checkout --` 复原、干净基线上同形失败，再复原 ⇒ 本机既有环境问题），以及 `cargo check --workspace --all-targets --all-features` 的 `STATUS_STACK_BUFFER_OVERRUN (0xc0000409)`（读 `build.rs` 后归因到既有的 `--all-targets` 破损）。

**P2-1 / P2-2 的口径澄清（2026-09-16）**：本件上面几段写的「P2-1 第一片 / 第二片已交付」「P2-2 已通路」，指的**都是 D13 / D14 这两张决策票的实现**（T14 / T15 / T16），也就是 P2-1 / P2-2 的**前置**——**不是** P2-1 / P2-2 本体。D13 的 Question 只覆盖「那三个比率的数据从哪取」（`metrics-collection-points`），D14 只覆盖「review 住在图上的哪里」（`review-plane-ownership`）。因此须明确两点：① **Optimizer 的算法**（用 Useful Work Ratio / Coordination Ratio / Activation Inflation *算什么*、输出给谁、谁来消费）与 **review 的策略**（审什么、审几轮、失败怎么重试）在仓内**均无更细定义**，三个指标名只在本件里被引用、**公式无据**；二者与 P2-3（market scheduling）一样，需要外部「方案第 21 节」原文才能开票 —— 本件的口径是**不臆造**。② 本轮的**真实成果是「阻塞解除」**（两项的前置已交付），**本体尚未开票**。另需记一项 v2 决策：**#666（多步 workflow 的逐步用量落在哪一层）gate 着 P2-1 的数据正确性** —— 多步链上报的是**最后一步**的用量（`python/ultimate_coders/agent/worker.py:1487-1500` 的注释自陈 *"an under-report of a multi-step chain, not a total"*，且同函数内 `file_changes` 是累积的、`token_usage` 不是 ⇒ 不累积是**刻意的**选择而非技术限制），而 `usage_reported: true` 让下游以为那是完整用量，**D13 设计的「覆盖率」机制只能发现「完全没上报」的节点、发现不了这种失真**。
**T17 交付（2026-09-16）**：D15 已裁 **B + C** 并落成实现票 T17 #667（任务归档 `09-16-t17-mirror-write-guard`；票面 `fdef55a`、实现 `1e61318`、测试补充 `c6dfd20`；`1e61318` 与 `c6dfd20` 上 Rust CI 各 **8/8 绿**）。落地形状：① `state` 从 shadow 分支的 `DO UPDATE SET` 中**移除** —— 这处删除本身就是 B；② 新增纯函数 `mirror_write_allowed(prev, mirror) = prev == mirror || transition_ok(prev, mirror)` 作为**已存在** node 的转移守卫（C），先一次 `SELECT (node_id, state)` 预取（非逐节点查询），守卫拒则整体 `continue`；③ `BackfillStats` 加 `mirror_state_dropped` / `mirror_rejected` 两个**互斥**计数（故 `rejected + dropped` 恰读作「快照与权威不一致的 node 数」）并加 `tracing::warn!` —— 因为「守卫拒了」与「镜像没到」在 DB 上**完全同形**（都不动 `state`），返回值与日志是唯一能区分之处，这正是 D15 待裁第 3 条要的可观测性。**复用既有 `transition_ok` 而非新写状态机**：铁律「图 node state 只能由一份依赖感知规则产出」约束的是 `dependency_aware_state`（一个 node **应当**是什么态），而「这一步迁移是否合法」是另一份规则，且已被 `commit_once` / `cancel` / `claim` / `schedule_attempt` 共用 ⇒ 这是把镜像路径接入同一份规则，不是制造第二份。
**一处过度声称已更正，且更正手段是消融而非推理（本件据此改口径）**：票面与源码 doc 曾把两条子句写成「形状 1（权威 `READY` + 迟到 `RUNNING`）靠 **C** 拦住回退、形状 3（权威 `RUNNING` + 迟到 `READY`）靠 **B** 拦住回退」，读起来像各守一半。**一次只删一条子句、重跑该集成测试**后得到不同答案：(a) **删掉 B** ⇒ 形状 3 打红（权威被回退），形状 1/2 仍绿；(b) **删掉 C** ⇒ 只在形状 1 的**计数断言**打红，**更靠前的 `state` 断言通过** ⇒ 权威 `state` 没动。准确口径因此是：**B 是 `state` 的保证**（B 在，任何镜像写入都动不了 `state`，无论守卫怎么判），**C 是一致性 + 可观测性守卫**（它拦掉已知不自洽的快照，使其连非状态列都写不进，并把这件事变成可计数而非静默的部分写入）。两者仍都必须有：「只留 C」在形状 3 破防（合法边 + 陈旧快照），「只留 B」让陈旧快照仍能静默改写结构且不可见 —— 这才是 D15 裁 B + C 的**准确**含义。
**一处与直觉相反的既有事实（本票首先被自己的单测打红才发现）**：`READY → RUNNING` **不是**合法边（本仓派发走 `READY → SCHEDULED → RUNNING`）⇒ `transition_ok("READY","RUNNING") == false`，故 **#664 的原始形状恰恰是被 C 拒掉的**；而 `RUNNING → READY`（fence re-arm）**是**合法边、C 放行，只能靠 B 拦住。另：`transition_ok` 按设计**拒自环**，故守卫第一臂 `prev == mirror` **不可省** —— 只取转移判定会把镜像的常规补列路径**整体误拒**，而那种失效的外观是「什么都没发生」。**门禁**：fmt / `clippy --workspace -D warnings` / `clippy -p uc-engine --all-targets --features storage -D warnings` / `check --workspace --all-targets --all-features` 全 clean；Rust 基线只增不减（uc-engine lib **446→447**、nodefault **386→387**、uc-grpc `--all-features` **238** 与 T16 一致 ⇒ 无 arity 回归）。**真实 PG：`graph_store_integration` 20 passed / 0 failed**（T15 基线 19 ⇒ +1），CI 的 `storage integration tests` 日志逐字出现 `test graph_t17_mirror_never_rolls_authority_back_or_resurrects ... ok` 且无 `SKIP:` 行 ⇒ **真跑**。⚠️ 本轮与 T14–T16 不同：**本地 live PG 通路可用**（`wsl.exe` 仍被程序黑名单拦住，但 PG 已在 `127.0.0.1:5432` 监听），故本轮**不存在「未实跑」欠账**。
**测试的五个形状与两处刻意的设计**：#664 原形（`READY`+`RUNNING`，拒）/ 复活（`SUCCEEDED`+`RUNNING`，拒）/ fence re-arm（`RUNNING`+`READY`，放行但 `state` 不动）/ 前向完成（`RUNNING`+`SUCCEEDED`，同上 —— 票面验收 2 点名的形状）/ 常规一致（两计数皆 0）。两处要点值得记下：① 权威行**刻意**用 `dependencies = ["ghost-dep"]` 播种（与 `state` 故意不自洽），否则**空**依赖集会让「被跳过」与「正常写入」产出**同一个 DB 状态**、测试为错误的原因变绿 —— 「写入是否真的落地」必须可观测；② 形状 4 额外显式钉住「`Completed` 快照仍会追加一行 `node_completions`，而 B 把 `state` 钉在 `RUNNING`」这一**不对称**（守卫只管 `state`，attempts / completions 仍是 append-only，D15 范围外）。把这个不对称写成断言，是为了让后来者**读到**它而不是**撞到**它 —— 它也正是「图平面的 `state` 是读者唯一可信之物」的具体理由。

**P2-1 的第三片已交付（2026-09-16，T18 #668，执行 #666 裁决 C）**：多步 workflow 的**逐步用量**落到终态事件 `node_succeeded` 的 `payload.steps[]`，纯加性（无新列、无新事件类型、零迁移、`contract_version` 未动；**没有 `steps` 键即「无可记录」**）。这解除了本件上段记的「#666 gate 着 P2-1 的数据正确性」：原先 `_execute_steps` 的**每一条** return 都只透传**一步**的 usage（同函数内 `all_file_changes` 累积而 `token_usage` 不累积 ⇒ **刻意**），而终态事件仍声明 `usage_reported: true`，故下游会把**最后一步**的量读成整节点的总量；D13 的覆盖率机制只能发现「完全没上报」的节点，**发现不了这种失真**。四条新增设计决定：① **一条 = 一个已执行的单元** —— 被条件跳过的步**不留条目**，`step_index` 的**跳号**即「那一步没执行」的编码（若给跳过的步发 `usage: null`，则同一个值兼表「跳过」与「跑了但没报」两种事实，而验收 2 要的恰是后者**可读**）；② `source` = 实测戳 `usage.source`（T15 在适配器解析点自盖）→ 回落该步声明的 `agent` → `null`（回落是报不出数字时唯一还能点名之处）；③ **步条目用显式 `null`、`SubtaskUsage` 沿用省略键 —— 两套相反纪律刻意并存**：前者是数组的**位置元素**（显式 `null` 让 `steps[i].usage` 恒可索引、不必先判存在），后者是**线格式增量**（缺键 = 未上报，且是 T15「不设 usage 的发布者序列化字节不变」的实现手段）；两者不相撞的**前提**是 payload 只写一次、**不会被反序列化回领域类型**（该前提已单记进 `implement.jsonl` 的 `boundary:D3-precondition`，以免将来变成隐形陷阱）；④ **legacy 单 agent 路径也发一条**（`step_index: 0`）—— decomposer 对简单 subtask **省略** `steps`（`orchestrator.ts:2093-2099`），那才是**多数**形状，故这是验收 5 的要求而非体贴；键取 `subtask.steps` 是否为空，**不是**取「收集到的列表为空」（全部步被跳过的 workflow **合法地**什么都不收集，在那里合成会凭空造出一个从未执行的单元）。

**T18 的两处方法要点**：① 把拆解逻辑抽成**未门控**纯函数 `steps_payload`，使其能在 `--no-default-features` 下被测（形状回归最难被发现之处），并**当场回本** —— `cargo check --workspace --all-features --all-targets` 抓到两处 `on_commit` 调用点的 arity 破窗，而它们所在文件带 `storage` 门控、默认特征下被编译成**空文件**，不带 `--all-features` 的 `cargo check --workspace` 完全看不见（与 T15 漏掉 5 处 `commit_once` 调用点是同一类失效面）。② **一次账目更正**：记忆中的本地 Python 基线「1120 passed」与实测不符；用 `git worktree` 在 **HEAD 上复跑**得到真实值 **1123 passed / 10 skipped**（1133 收集），并进一步用**总收集数对账**（本地 `1139+10=1149` vs CI `1141+8=1149` 完全一致，差 2 条为 POSIX-only）与**测试 ID 集 `comm`**（removed = 0 / added = 16，逐条皆本票新增）双重钉住「没有测试被静默丢掉」。消融自检（一次只删一条子句）：删 `steps_payload` 的空切片早返回 ⇒ **仅** `steps_payload_is_absent_when_there_is_nothing_to_record` 打红；给 `StepUsage.usage` 加 `skip_serializing_if` ⇒ uc-engine 与 uc-types **各在一个 crate 打红一条**，证明 D3 的两条断言确系**互不依赖**而非同一断言抄两遍。

**T18 的门禁与 CI（2026-09-16，`8a0645d`）**：本地 fmt / clippy（`--workspace --all-targets --all-features -D warnings` 与 CI 原样的 `--workspace -D warnings`）/ `cargo check --workspace --all-features --all-targets` / 四组 cargo test 全绿；Rust 基线**只增不减**（uc-engine lib **447→452** 默认、**387→392** no default、**467** all-features；uc-grpc lib **210→212**、**238→240**；uc-types **47→50**；ignored 图套件 **20→21**）；Python 本地 **1123→1139 passed / 10 skipped**；ruff 与 dashboard 导入检查 clean。**CI：Rust CI 8/8、Python CI 4/4 全绿**（TypeScript CI **未被触发** —— 本 diff 不含 `packages/**`，符合其路径过滤）。CI 日志里 `467 / 240 / 50 / 36 filtered out` 四行还**独立复核**了本地 all-features 计数。⚠️ **本机 PG 本轮不可用**：provider 是 Docker Desktop，其引擎在本沙箱**起不来**（`docker desktop start` 报 starting 后 `docker info` 以 30×5s 轮询始终失败），故该 PG 测试**本地未跑**，唯一证据是 CI —— 判**真跑**的两条判据都过（日志逐字有 `test graph_t18_commit_carries_per_step_usage_into_the_terminal_payload ... ok`、同文件 `21 passed / 0 failed`、**全日志 0 条 `SKIP:`**）。这同时**更正**了 T17 段记的「本地 live PG 可用」：当时为真，但**不能跨轮沿用**（引擎停掉即消失），正确口径是「本机无原生 PG，live PG 取决于 Docker Desktop 是否在跑」。
