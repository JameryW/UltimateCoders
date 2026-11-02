# 治理层（Governance Plane）：PR01–PR06 设计与验收

本目录记录 UltimateCoders 工程 Agent OS 升级中 **PR01–PR06** 的治理层设计与运维边界。

治理层回答两个问题：**为什么做**、**在什么授权下做**。它不回答「怎么做」——执行仍然是
`Task → Subtask → DAG(Node) → Attempt → Worker → EffectLedger → Oracle` 这条既有链路
（`crates/uc-types/src/agent.rs`、`crates/uc-engine/src/graph_store.rs`）。

```
治理层（DeepSeek 借鉴）—— 决定「为什么做 / 凭什么做」

  Source ──► Trigger ──► Policy ──► Mandate ──► WorkCase ──► Subscription ──► Attention
              去重收件箱   权限判定    授权范围     长任务单元      事件订阅        人/等待投影
                 │            │           │
                 │            │           └─ 授权只能收窄，且必须来自人类签发
                 │            └─ 结构性天花板 PrincipalKind::may_hold（不可配置）
                 └─ 触发器不带授权：只能让工作被注意到

  ───────────────────────────────────────────────────────────────────────────────────────
        │ 提交：PR01 submit-once（稳定 submission_id → 派生的 task_id，最多一个逻辑 task）
        │ 链接：PR02 CaseRun.task_id（case 拥有多次 run，但不持有 DAG）
        ▼

执行层（UC 内核，未被替换）—— 决定「怎么做」

  TaskRun ──► DAG/GraphStore ──► Node ──► Attempt ──► Worker(Sandbox) ──► EffectLedger ──► Oracle
                 Rust 是 DAG 唯一权威        NATS 是派发传输        授权来自 Mandate
```

上图对应 `crates/uc-types/src/governance/mod.rs:1-33` 的模块文档：第二层**原样保留**
（`Task`/`Subtask`/`SubtaskStatus` 仍是执行契约，Rust `GraphStore` 仍是 DAG 权威，NATS 仍是派发
传输），治理层的 `WorkCase` 只是**拥有多个 TaskRun 的治理实体**，不替代调度器。

## 1. 为什么是「一层」而不是「另一套系统」

治理层刻意做得薄，因为它站在一个已经很重的执行系统旁边：

| 复用点 | 说明 | 出处 |
| --- | --- | --- |
| 持久化 | 所有治理实体都是「命名空间 + key + CAS 版本」的 JSON 文档，落在同一张表 `governance_records`，不再为每个实体加迁移 | `crates/uc-engine/src/governance/record_store.rs:236-248` |
| 与 Python 的一致性 | 该形状对齐 `python/ultimate_coders/runtime_state.py` 的文档存储（SQLite/PostgreSQL 可互换） | `crates/uc-engine/src/governance/record_store.rs:1-30` |
| 决策可测试 | 权限/授权/注意力判定全在 `uc-types` 的纯函数里，`now` 由调用方传入，不读时钟、不做 I/O、不调模型 | `crates/uc-types/src/governance/mod.rs:10-14` |
| 执行契约 | 治理层从不携带 DAG；`WorkCase` 只存 `mandate_id`/`run` 链接与 `completion_conditions`（不透明 JSON） | `crates/uc-types/src/governance/workcase.rs:319-362` |

一句话：**治理层决定「是否、为何、凭何」，执行层决定「如何」。** 所以本目录里没有第二套
调度器、没有第二份任务状态、也没有第二个权威。

## 2. 六项验收结果（含反例对照）

六项验收不是标语，每一项都能指到一个「总函数」或一段可失败的测试。下表是索引，后面逐项展开。

| 验收项 | 一句话判据 | 权威实现 | 证明它的测试 |
| --- | --- | --- | --- |
| 不重复 | 一个 `submission_id` 最多产生一个逻辑 task；同一 thread 最多一个 case；同一 webhook 只入账一次 | `crates/uc-types/src/governance/submission.rs`、`crates/uc-engine/src/governance/submission.rs` | `derived_task_id_is_stable_across_retries`、`retry_returns_the_same_task_and_does_not_duplicate_the_outbox_entry`、`create_unique_converges_on_the_same_thread`、`redelivery_is_reported_as_duplicate_and_matches_nothing` |
| 不越权 | 外部事件/匿名调用永远不能持有授权类权限；mandate 只能收窄 | `crates/uc-types/src/governance/policy.rs`（`PrincipalKind::may_hold`）、`mandate.rs`（`validate_child`） | `external_trigger_cannot_hold_authority_granting_permission`、`agent_cannot_scale_workers`、`delegation_cannot_add_authority`、`delegated_child_claiming_human_origin_is_refused` |
| 可持续 | case 跨多次 run/重启存活，run 历史只追加且有界 | `crates/uc-types/src/governance/workcase.rs`、`crates/uc-engine/src/governance/case_store.rs` | `run_history_is_bounded_but_counted`、`open_and_finish_run_drives_the_state_machine`、`a_matched_event_wakes_the_case_and_resolves_its_attention` |
| 可恢复 | 崩溃点之后不丢提交、不产生第二个 task；注意力项不是锁 | `crates/uc-engine/src/governance/outbox.rs`、`crates/uc-engine/src/governance/submission.rs` | `in_flight_entry_is_recovered_after_crash`、`crash_between_record_and_outbox_is_recovered_with_the_same_task_id`、`recover_requeues_entries_orphaned_in_flight` |
| 少打扰 | 等待外部事件不占 Worker 槽位、不打扰人；只有人工决策有优先容量 | `crates/uc-types/src/governance/attention.rs`（`holds_worker_slot`/`reserves_human_slot`）、`python/ultimate_coders/attention/capacity.py` | `waiting_for_ci_never_holds_a_worker_slot`、`only_needs_human_interrupts_a_human`、`worklist_puts_human_decisions_first_then_priority`、`capacity_reserves_human_headroom_and_reports_savings` |
| 可验证 | 每个放行/拒绝都留可审计记录，提案必须带证据 | `crates/uc-types/src/governance/proactive.rs`（`ProactiveAuditEntry`）、`crates/uc-engine/src/governance/watchdog.rs` | `audit_entry_always_records_deny_reason`、`proposal_without_evidence_is_refused`、`replan_preserves_confirmed_effects_for_audit` |

### 2.1 不重复

三条独立机制，分别对应三种「重复」：

1. **同一提交重复** — task id 由 submission id **派生**（`derive_task_id`，
   `crates/uc-types/src/governance/submission.rs:128-131`），所以重试、重连、重启恢复都指向同一个逻辑
   task；`submit_once` 用 create-only CAS（`Some(0)`）抢占记录，冲突时比对内容摘要，一致返回
   `Replayed`、不一致返回 `Conflict`（`crates/uc-engine/src/governance/submission.rs:94-133`）。
2. **同一事件重复投递** — 去重键只取 `(source, kind, external_id, payload_digest)`，不含接收时间
   （`trigger.rs:236-245`）；收件箱以 create-only CAS 落库，第二份直接报 `duplicate` 且不再扇出
   （`crates/uc-engine/src/governance/trigger_inbox.rs:77-96`）。
3. **同一线程重复建 case** — `CaseId::from_parts(source, external_ref)` 是确定性 id
   （`workcase.rs:41-47`）。
   `CaseStore::create_unique` 冲突即返回既有 case（`case_store.rs:83-99`）。

### 2.2 不越权

结构性天花板是硬编码的，不是配置项（`policy.rs:56-82`）：

| principal 种类 | 可持有的权限 |
| --- | --- |
| `user` / `service` | 全部 15 个 |
| `agent` / `worker` | 全部，**除去** `mandate_issue`、`mandate_revoke`、`worker_scale` |
| `external_trigger` / `anonymous` | 只能持有**非授权类**权限（`is_authority_granting()` 为假的那些） |

授权类权限固定为 5 个：`mandate_issue`、`mandate_revoke`、`worker_scale`、`merge_grant`、
`proactive_execute`（`policy.rs:190-202`）。违规的 grant 在**构造快照时**就被拒绝
（`PolicySnapshot::with_grant` → `GrantError::ForbiddenPrincipalKind`，`policy.rs:506-517`），而不是
指望评估器事后发现。第二重是 mandate 只能收窄：不存在「扩权」操作，`validate_child` 逐维度检查
子授权是父授权的真子集（`mandate.rs:534-635`）。

### 2.3 可持续

`WorkCase.runs` 只追加、按 `attempt` 递增，且 `MAX_RUN_HISTORY = 64`
（`workcase.rs:28-30`）；压缩只搬走**已结束**的 run 并把数量记进 `compacted_runs`，`attempt`
编号由 `compacted_runs + runs.len() + 1` 续上，所以审计不会因为压缩而出现「attempt 跳号且无解释」
（`workcase.rs:417-419`、`workcase.rs:503-514`）。状态机另有硬约束：`Active → Completed` 恒为
`false`，成功必须先进入 `verifying`（`workcase.rs:165-208`）。

### 2.4 可恢复

- 出站记录**先写**，再碰传输；崩溃只会留下证据，不会静默丢弃
  （`crates/uc-engine/src/governance/outbox.rs:1-16`）。
- 传输中被崩溃打断的 `in_flight` 条目在启动时回到 `pending` 并立即到期
  （`outbox.rs:141-160`），代价是**可能重复投递**，所以消费端必须按同一身份去重。
- 提交侧留有两条恢复路径：`submit_once` 的重放分支会补投 outbox；`recover_unfinished` 对
  `received`/`published` 的记录用**同一个** task id 重新排队
  （`crates/uc-engine/src/governance/submission.rs:248-300`）。
- 注意力项是**投影不是锁**：`resolve` 幂等、可重建，调度器不依赖它的存在
  （`crates/uc-engine/src/governance/attention.rs:1-12`、`attention.rs:113-138`）。

### 2.5 少打扰

`AttentionKind::holds_worker_slot()` 是**总函数**：四种 kind 全部返回 `false`
（`attention.rs:63-68`）——注意力状态的定义就是「这个 case 没有在执行」。真正会占人类注意力
的只有 `needs_human`（`requires_human_now`，`attention.rs:86-88`），也只有它预留人类优先容量
（`reserves_human_slot`，`attention.rs:77-79`）。`blocked` 上台账但**不**打断人：它按定义不是
「某个决定能立刻解开的东西」。

### 2.6 可验证

- 每个 gate 判定——允许**和**拒绝——都写一条审计记录
  （`ProactiveAuditEntry::from_decision`，`proactive.rs:451-461`；
  `ProactiveGate::submit` 与 `admit_execution` 都先落审计再返回，
  `crates/uc-engine/src/governance/proactive.rs:171-254`）。
- 提案没有证据就不成立：`admit_proposal` 在 `evidence.is_empty()` 时拒绝，动作名为
  `propose_without_evidence`（`proactive.rs:358-384`）。
- watchdog 的每个决定都落 `watchdog_decision` 命名空间，并带上 `preserve_effects`
  （`crates/uc-engine/src/governance/watchdog.rs:101-122`），事后能回答「当时为什么重规划」。
- 治理文档本身带 CAS 版本（`Record.version`），可以用来判断两条记录谁更新
  （`record_store.rs:32-53`）。

## 3. 文档索引

| 文件 | 内容 |
| --- | --- |
| [pr01-submit-once.md](pr01-submit-once.md) | submit-once 协议：稳定提交身份、派生 task id、事务外发箱、崩溃恢复、消费端幂等；含状态机与失败矩阵 |
| [pr02-workcase.md](pr02-workcase.md) | WorkCase / CaseRun / Mandate：状态机、成功必须经 `verifying`、run 历史、嵌套规则、与既有 Contract / Completion Conditions 的组合 |
| [pr03-policy.md](pr03-policy.md) | principal / permission / grant 模型、结构性天花板表、工具白名单策略、沙箱硬化档、ScaleWorkers 授权、吊销与策略版本陈旧 |
| [pr04-triggers.md](pr04-triggers.md) | 触发收件箱与订阅：去重键推导、线程匹配、GitHub webhook 与轮询对账的收敛、为什么触发器不带授权 |
| [pr05-attention.md](pr05-attention.md) | 注意力种类、为什么等待不占槽位、人类优先预留、工作清单排序、容量指标 |
| [pr06-proactive.md](pr06-proactive.md) | Off/Observe/Propose/Execute、证据要求、审计留痕、watchdog 边界、「模型可建议、控制面决定」 |
| [operations.md](operations.md) | 环境变量与安全默认值、带治理面启动、逐 PR 手工验收、升级顺序 |

## 4. 未实现 / 有意留空

写文档时按「代码里没有就不写」的原则处理，以下是**已知的空白**，以及为什么现在留空：

| 空白 | 现状 | 原因 |
| --- | --- | --- |
| gRPC 服务与 NATS 桥的 Rust 侧实现 | **已落地**：`crates/uc-grpc/proto/engine.proto` 的 `CaseService`/`TriggerService`/`AttentionService`/`ProactiveService`，对应实现 `crates/uc-grpc/src/governance/*.rs`，NATS 请求/回复桥（27 个 op）+ outbox 分发器 `bridge.rs`，网关接线 `GrpcServer::with_governance` / `into_governance_services` 与启动引导 `crates/uc-grpc-server/src/governance_boot.rs`。Python 端请求方为 `python/ultimate_coders/governance_client.py` | **以 proto 为权威**；桥接只镜像同一批 op（一份实现，两种传输） |
| Graph-backed fencing 纳入治理 | 未纳入。`WorkCase` 明确不存 DAG | 执行面租约/fencing 已有独立设计（见 `docs/architecture.md` 的 Durable Runtime 一节），治理层重复实现会产生第二权威 |
| 消费端去重（`uc.task.submit` 订阅方） | **已落地**：`python/ultimate_coders/submission_dedup.py`（`(submission_id, task_id)` 持久记录 + `observe`/`mark_applied`）接入 `nats_worker._handle_submit`；重复投递被丢弃并回 `duplicate:true`，崩溃在 `observe` 与 `mark_applied` 之间时**重放而不是丢弃**。执行面 `TaskStore` 仍是主键兜底 | 归执行面改造；治理层只保证**身份稳定**并把 `submission_id`/`task_id` 放进 payload |
| 交互式审批流（提案 → 人工确认 → 执行） | `human_approval` 已强制但形式是「操作者层级即批准」：`scale_workers` / `docker_run` 额外要求 `proactive_execute`，agent/worker 被结构性天花板挡住（`deny_code=tool_requires_human_approval`）；没有交互式确认队列 | 真实审批通道属于 PR06 的 proactive 面；在 UI 与队列落地前，把「谁有权触发」收紧到操作者层级是唯一可强制的边界 |
| 非 GitHub 的 webhook 签名方案 | 未实现。`SourceKind` 有 `ci`/`lark`，但只有 GitHub 的 `sha256=` HMAC 校验 | 各 provider 的签名算法不同，猜一个比不支持更危险 |
| 多主机调度 / 容量拍卖 | 不在本层 | 属于既有 scheduler + `UC_SCALE_HOSTS` 的职责，见 `AGENTS.md` |
| `UC_GOVERNANCE_BACKEND` 的读取点 | **已落地**：`crates/uc-grpc-server/src/governance_boot.rs::choose_record_store`（`memory` / `postgres` / `pg`；未知值降级为内存并 warning） | 见 [operations.md](operations.md) §1 |

文档状态：按工作树上 `crates/uc-types/src/governance/`、`crates/uc-engine/src/governance/`、
`crates/uc-grpc/proto/engine.proto`、`python/ultimate_coders/{trigger,attention,proactive}/` 与
`python/ultimate_coders/dashboard/*_api.py` 的实际内容撰写。若上述并行改动已落地，**以代码为准**，
本目录应同步更新——尤其第 4 节表格。

## 5. 术语

| 术语 | 含义 | 定义处 |
| --- | --- | --- |
| submission | 一次「创建一个逻辑 task」的请求；带稳定 id | `crates/uc-types/src/governance/submission.rs:180-230` |
| derived task id | 由 submission id 派生的 task 身份，形如 `sub-<32hex>` | `submission.rs:128-131` |
| outbox | 落库后再投递的出站队列（at-least-once） | `crates/uc-engine/src/governance/outbox.rs` |
| WorkCase | 跨多次 run/CI 周期/重启的长任务单元 | `crates/uc-types/src/governance/workcase.rs:319-362` |
| CaseRun | case 的一次执行尝试，链接到一个 `task_id` | `workcase.rs:257-305` |
| mandate | 窄的、显式的、可吊销、有时限的授权集合 | `crates/uc-types/src/governance/mandate.rs:172-212` |
| mandate 的 origin | `human_issued` / `case_delegated` / `system_default` / `test_fixture` | `mandate.rs:140-170` |
| trigger | 外部事件（去重后入账），不带授权 | `crates/uc-types/src/governance/trigger.rs:157-246` |
| subscription | case 对某事件流的持续兴趣 | `trigger.rs:338-379` |
| attention item | 「这个 case 为什么停住」的投影 | `crates/uc-types/src/governance/attention.rs:131-228` |
| policy snapshot | 某一策略版本下的完整授权状态 | `crates/uc-types/src/governance/policy.rs:475-590` |
| proactive proposal | 带证据的建议；本身不含执行权 | `crates/uc-types/src/governance/proactive.rs:168-304` |
| watchdog verdict | 对停滞的**有界**处置：continue / retry / replan / escalate / stop | `proactive.rs:565-593` |

> 约定：本目录所有「引用了某个行为」的断言，都能在标注的文件里找到对应代码或测试。若发现文档与
> 代码不一致，视为文档 bug。
