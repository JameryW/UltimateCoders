# PR02 — WorkCase / CaseRun / Mandate：跨多次运行的长任务

代码：`crates/uc-types/src/governance/workcase.rs`（case 与 run 的纯状态机）、
`crates/uc-types/src/governance/mandate.rs`（授权范围与嵌套）、
`crates/uc-engine/src/governance/case_store.rs`（`CaseStore` / `MandateStore`，CAS 持久化）。
proto：`crates/uc-grpc/proto/engine.proto:954-1198`（`CaseService` + `CaseProto`/`CaseRunProto`/`MandateProto`）。

## 1. 定位：case 记录「为什么」，不记录「怎么做」

模块文档原话：`WorkCase` 是「在 `TaskRun` 之间活下来的东西」——`Task` 仍然是一次执行请求，
case 记录**为什么这份工作存在**（来源 trigger）、**在什么授权下**（mandate）、**已经试过什么**
（run 历史）。它**刻意不含 DAG**：图平面仍是执行规划的唯一权威
（`workcase.rs:1-20`）。

这条边界决定了三件事：

1. 治理层不会为了「跨运行」再造一个调度器；
2. `CaseRun.task_id` 是**链接**，不是副本——`task_id` 属于执行面；
3. 任何「必须看 DAG 才能回答的问题」都不该问 case，应该去 `GraphStore`。

## 2. 数据模型

### 2.1 WorkCase（`workcase.rs:319-362`）

| 字段 | 作用 | 注意 |
| --- | --- | --- |
| `id: CaseId` | 身份 | `from_parts(source, external_ref)` 确定性派生（见 §2.2） |
| `project_id` | 执行范围 | case 的每次 run 都被限制在该范围内 |
| `status: CaseStatus` | 治理状态 | 9 态，见 §3 |
| `origin: CaseOrigin` | 来源 | `source` + `external_ref` + `thread_key` + `trigger_id`；`manual()` 表示人直接开 |
| `contract_digest` | 原始人类请求的规范化摘要 | 与 worker 侧 `python/ultimate_coders/agent/contract.py` 的摘要对齐 |
| `completion_conditions` | 完成条件的**不透明 JSON** | 求值归执行面（`python/ultimate_coders/agent/completion_conditions.py`） |
| `mandate_id` | 当前有效授权 | `None` = **该 case 不得执行任何东西** |
| `subscriptions` | 喂这个 case 的事件订阅 | PR04 |
| `runs: Vec<CaseRun>` | 只追加的运行历史，上限 `MAX_RUN_HISTORY = 64` | `compacted_runs` 记被压缩掉的数量 |
| `priority: u8` | 人类关注度（影响 PR05 工作清单顺序） | 由 `set_priority` 夹到 `MAX_PRIORITY = 9`（`workcase.rs:396-410`，测试 `priority_is_capped`），所以触发器无法自封极端优先级 |
| `version: u64` | 存储拥有的 CAS token | 单调递增 |

`runs` 的压缩规则（`workcase.rs:503-514`）：只压缩**已结束**的 run，且把计数写进
`compacted_runs`，因此 `attempt` 编号（`compacted_runs + runs.len() + 1`，
`workcase.rs:417-419`）不会因为压缩而跳号且无解释。测试：`run_history_is_bounded_but_counted`。

### 2.2 CaseId 的确定性（`workcase.rs:41-47`）

```
case_id = "case-" + sha256("case:{source}:{external_ref}")[..32]
```

于是「同一个 PR 的重复投递/轮询事件」收敛到同一个 case，而不是每次开新 case。
`CaseStore::create_unique` 用 create-only CAS 落库，冲突即返回既有 case
（`case_store.rs:83-99`）。
响应里的 `dedup_replayed` 见 `engine.proto:1058-1065`。
另有 `find_by_thread` 的线性扫描，专门兼容「早于确定性 id」的历史 case
（`case_store.rs:109-125`，注释说明为什么不做二级索引）。
测试：`create_unique_converges_on_the_same_thread`、`deterministic_case_identity_converges_for_same_thread`。

### 2.3 CaseRun（`workcase.rs:257-305`）

| 字段 | 含义 |
| --- | --- |
| `run_id` | 本次运行身份（UUID） |
| `task_id` | 执行面的逻辑 task（PR01 的派生 id 或外部传入） |
| `attempt` | case 内 1-based 单调编号 |
| `submission_id` | 创建这次运行的提交身份（幂等句柄，PR01） |
| `started_at` / `ended_at` / `outcome` / `detail` | 生命周期与原因 |

`CaseRunOutcome` 四值（`workcase.rs:307-317`）：`succeeded` / `failed` / `cancelled` / `superseded`。
`superseded` 专门表示「被更新的运行取代」（例如新的 commit 到达），它**不是失败**。

## 3. 状态机

### 3.1 九个状态（`CaseStatus`，`workcase.rs:86-111`）

| 状态 | 含义 | 是否占执行资源 | 是否终态 |
| --- | --- | --- | --- |
| `open` | 已创建，尚无 run | 否 | 否 |
| `active` | 有 run 在执行，或已排好继续 | 是 | 否 |
| `waiting_event` | 等外部事件（CI 结果、PR review、webhook） | **否** | 否 |
| `needs_human` | 等人做决定 | **否**（但预留人类优先容量） | 否 |
| `blocked` | 卡在既非事件也非人工决定的先决条件上（依赖失败、预算耗尽） | **否** | 否 |
| `verifying` | 执行自称完成，独立验证进行中 | **否** | 否 |
| `completed` | 独立验证通过 | 否 | **是** |
| `failed` | 验证失败 / 放弃 / 被策略终止 | 否 | **是** |
| `cancelled` | 被有权限的 principal 取消 | 否 | **是** |

`is_quiescent()`（`workcase.rs:151-160`）= `waiting_event | needs_human | blocked | verifying`，
这四种状态下 `open_run` 直接被拒（`workcase.rs:433-435`）。

### 3.2 允许的迁移（`can_transition_to`，`workcase.rs:162-208`）

```
open ──────► active / blocked / needs_human / failed / cancelled
active ────► waiting_event / needs_human / blocked / verifying / failed / cancelled
active ──x──► completed            （必须先 verifying）
waiting_event ──► active / needs_human / blocked / verifying / failed / cancelled
needs_human ────► active / blocked / failed / cancelled
blocked ────────► active / needs_human / waiting_event / failed / cancelled
verifying ──────► completed / failed / active（验证发现还有活）/ needs_human / cancelled
completed|failed|cancelled ──x──► 任何状态
```

同状态自迁移是允许的（幂等写入）；终态不可离开（`workcase.rs:167-169`）。
测试：`active_case_cannot_jump_to_completed`、`illegal_transition_is_rejected_without_writing`、
`every_status_round_trips_through_its_wire_token`。

### 3.3 为什么成功必须经过 `verifying`

因为「执行面说完成」和「工作真的完成」是两件事。执行面的 run outcome 是
`Succeeded`（`workcase.rs:310-312` 注释：*the execution plane reported success; the case still has
to verify*），`finish_run` 把它映射到 `Verifying`，而不是 `Completed`
（`workcase.rs:451-478`）：

| run outcome | 迁移到 | 说明 |
| --- | --- | --- |
| `succeeded` | `verifying` | 自称成功 ≠ 已验证成功 |
| `failed` | `active` | 允许同一 case 再试（重试/复跑仍在同一治理实体下） |
| `cancelled` | `cancelled` | 终态 |
| `superseded` | `active` | 被新运行替代，不算失败 |

`Verifying → Completed` 只能由**验证结论**推进；`Verifying → Active` 是「验证发现还有活」的
合法回路。测试：`success_goes_through_verifying_not_straight_to_completed`。

这条规则和既有验收文化一致：`docs/architecture.md` 的 Inference 验收一节也强调
「backend success alone is not correctness or performance evidence」。

## 4. 运行开合的四道闸

`open_run`（`workcase.rs:421-449`）按顺序拒绝，且每种拒绝有独立错误（`CaseTransitionError`，
`workcase.rs:517-527`）：

1. **终态** → `Terminal(status)`：终态 case 不得再开 run；
2. **静默态** → `Quiescent(status)`：`waiting_event`/`needs_human`/`blocked`/`verifying` 下不许开；
3. **已有未闭合 run** → `RunAlreadyOpen`：**一个 case 同时最多一个 open run**；
4. **没有 mandate** → `NoMandate`：无授权不得执行。

存储侧的 `open_run` 还会在 `status == open` 时先推进到 `active`，避免状态机出现
「有 run 但仍 open」的不自洽（`case_store.rs:163-181`）。
测试：`case_without_mandate_cannot_execute`、`only_one_run_may_be_open`、
`quiescent_case_cannot_open_a_run`、`open_and_finish_run_drives_the_state_machine`。

## 5. Mandate：授权是窄的、可吊销的、有时限的

`Mandate` 字段见 `mandate.rs:342-361`：`case_id`、`grantor`、`policy_version`、`scopes`、
`issued_at`、`expires_at`、`revoked`、`origin`、`delegated_from`。

### 5.1 七个授权维度（`MandateScope`，`mandate.rs:172-198`）

`repo` / `branch` / `path`（模式匹配）、`tool`（按名或 `*`）、`action`（`ActionClass`）、
`budget`（`max_tokens`/`max_cost_usd`/`max_wall_secs`）、`max_runs`。

两条反直觉但关键的规则：

- **模式匹配保守**：`*` 只匹配单段，`**` 跨段，**证明不了就算未覆盖**（`mandate.rs:20-31`）；
- **预算缺省不等于无限**：`budget` scope 里没写的维度视为**未授权**，请求该维度直接拒
  （`mandate.rs:186-195`、`mandate.rs:480-506`）。

### 5.2 检查顺序（`Mandate::permits`，`mandate.rs:418-517`）

```
吊销? ──► 过期? ──► repo ─► branch ─► path ─► tool ─► action ─► tokens ─► cost ─► wall ─► runs
   │         │
   └─ 拒绝 ──┴─ 拒绝        （先查时效，再查范围：缓存的旧 mandate 不能因为范围恰好匹配而可用）
```

请求里每个「出现了的维度」都必须被至少一个 scope 覆盖；没出现的维度不检查。
`MandateRequest::default()`（全空）因此是「只验时效」的探针——`validate_child` 正是用它先确认父
授权本身仍有效（`mandate.rs:540-542`）。

### 5.3 嵌套规则（`validate_child`，`mandate.rs:525-635`）

子授权相对父授权**只能更窄**，逐条硬检查：

| 规则 | 拒绝时的 denial |
| --- | --- |
| 父授权自身必须仍有效（未吊销未过期） | 父的原始 denial 透传 |
| 子不得声明 `human_issued` origin（否则组件能自造根授权） | `OriginForbidden` |
| 子 `policy_version` 不得低于父 | `StalePolicyVersion` |
| 子 `policy_version` 不得低于调用方给出的最低策略版本 | `StalePolicyVersion` |
| 子必须指向父（同 `case_id` 或 `delegated_from == parent.id`） | `WideningDenied` |
| 子有效期不得晚于父，也不得抹掉父的有效期 | `WideningDenied` |
| 子每个 scope 必须被父覆盖（`repo`/`branch`/`path` 用 `pattern_subsumes`；`tool` 精确或父为 `*`；`action` 相等；`budget` 各维更小；`max_runs` 更小） | `WideningDenied` |

测试：`delegation_cannot_add_authority`、`delegated_child_claiming_human_origin_is_refused`、
`narrower_child_is_accepted`、`child_cannot_outlive_parent_or_use_older_policy`、
`missing_scope_kind_is_denied_not_unrestricted`、
`expired_and_revoked_mandates_are_denied_before_scope_check`。

### 5.4 交集而不是并集

一个 case 可能从多处累积授权（例如人类追发一次更窄的许可）。`Mandate::intersect`
取**最窄**组合：逐个 scope 取两边都有的，budget 取各维最小，有效期取更早
（`mandate.rs:637-700`）。理由写在注释里：交集永远不会比任一输入更宽
（测试：`intersection_is_never_wider_than_either_input`）。

### 5.5 发证与吊销（`PolicyEngine`，`crates/uc-engine/src/governance/policy.rs:271-390`）

`issue_mandate` 的顺序：① 授予者必须持有 `mandate_issue`（按项目范围）；② 若是委派，必须通过
`validate_child`；③ 同一 case 不得同时存在另一份**有效** mandate（`MandateConflict`）——这条防止
「多份授权叠出更大权限」。委派出来的 mandate 会**落到父的 case**上（`policy.rs:329-331`）：
授权随工作流动，不制造第二个受治理的 case。

`revoke_mandate` 除了写回存储，还会把吊销记进当前策略快照的 `revoked_mandates`，让在途检查快速失败
（`policy.rs:366-370`）；吊销幂等且粘性（测试 `mandate_revocation_is_idempotent_and_sticky`）。

## 6. 与既有 Contract / Completion Conditions 的组合

不是替换，是分层：

| 层 | 谁拥有 | 在 case 里怎么体现 |
| --- | --- | --- |
| 请求契约 `contract_digest` | worker 侧 `python/ultimate_coders/agent/contract.py` 的规范化摘要 | 作为 `WorkCase.contract_digest` 保存，用于判断「case 的人类意图是否变了」 |
| 完成条件 `completion_conditions` | 执行面 `python/ultimate_coders/agent/completion_conditions.py`（默认不强制，`UC_COMPLETION_CONDITIONS_ENFORCE` 关闭） | case 只存**不透明 JSON**，求值仍在执行面 |
| 运行结果 | 执行面 `TaskStore`/`GraphStore` | `CaseRun.outcome` 只是摘要；细节去执行面查 |
| 授权 | 治理层 mandate | 执行面每次动工具/写路径前的判定（PR03） |

换句话说：**case 不做验收，case 记「有没有验收过」**。这也是 `Verifying` 状态存在的原因——它是把
执行面的验收信号翻译成治理状态的那一格。

## 7. 验收：怎么手工验

1. `cargo test -p uc-types governance::workcase`（状态机与 run 历史）；
   `cargo test -p uc-types governance::mandate`（授权收窄与交集）。
2. `cargo test -p uc-engine governance::case_store`
   （`create_unique_converges_on_the_same_thread`、`case_without_mandate_cannot_open_a_run`、
   `illegal_transition_is_rejected_without_writing`、`latest_mandate_for_case_is_the_newest_one`）。
3. gRPC 可用后：对同一 `thread_key` 连发两次 `CreateCase` → 第二次 `dedup_replayed = true` 且
   `case.id` 相同；随后 `TransitionCase(completed)` 直接跳级必须失败，而
   `TransitionCase(verifying)` 成功（`engine.proto:1090-1102`）。
4. 手工确认「无 mandate 不能跑」：对没有 `mandate_id` 的 case 调 `OpenCaseRun`，应返回
   `case has no mandate` 类错误（`CaseTransitionError::NoMandate`）。

## 8. 未实现 / 有意留空

| 项 | 现状 | 原因 |
| --- | --- | --- |
| case 内并发 run | 明确禁止（`RunAlreadyOpen`） | 并发 DAG 执行由执行面决定；治理层只保证「同一 case 同时只有一次受治理的尝试」 |
| case 级 DAG / 计划缓存 | 不存在 | `workcase.rs:1-20` 明确：图平面是唯一权威，重复会制造第二份计划 |
| `verifying` 的自动验收器 | 未实现；`Verifying` 需要一个外部主体（Orchestrator/CI/Oracle）推进 | 验收标准属于具体领域，治理层不内置判据 |
| 跨 case 的授权继承 | 未实现（`intersect` 只用于同 case 的多来源） | 跨 case 继承会让授权沿着 case 图扩散，违反「只能收窄」的可理解性 |
| `CaseStore::find_by_thread` 的索引 | 线性扫描（上限由调用方 limit 控制） | 活跃 case 数量在数十级，二级索引会成为第二个一致性负担（`case_store.rs:109-114` 注释） |
