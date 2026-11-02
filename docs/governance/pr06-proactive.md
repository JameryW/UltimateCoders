# PR06 — Proactive 与 Watchdog：模型可以建议，控制面决定

代码：`crates/uc-types/src/governance/proactive.rs`（模式、提案、gate、审计、watchdog 决策，纯函数）、
`crates/uc-engine/src/governance/{proactive,watchdog}.rs`（`ProactiveGate`、`Watchdog` 的持久化）、
`python/ultimate_coders/proactive/{__init__,modes,evaluator,watchdog_input}.py`（求值、模式解析、
停滞信号与动作映射）、`python/ultimate_coders/dashboard/proactive_api.py`（HTTP 面）。
proto：`crates/uc-grpc/proto/engine.proto:1384-1545`（`ProactiveService`，含 `ReportStall`/`ListAudit`）。

## 1. 一条硬规则，两处落地

**模型可以建议，控制面决定。** 这句话在本 PR 里对应两个互不重叠的关注点：

| 关注点 | 谁做 | 产出 |
| --- | --- | --- |
| 「什么值得做」 | Python `proactive/evaluator.py`（规则 + 证据） | `ProactiveProposal`（带证据的建议） |
| 「是否允许做」 | Rust `ProactiveGate`（模式、证据、容量、预算、审批） | `Admission`（allowed / deny_code+detail） |
| 「停滞了怎么办」 | Python `watchdog_input.py` 产信号；Rust `WatchdogConfig::decide` 判处置 | `WatchdogVerdict`（有界） |

Python 侧不能准入、不能花 token、不能落决策、不能执行任何工作（`proactive/__init__.py` 模块文档）；
Rust 侧不会自己想出建议。两侧通过 `proactive.proposal.submit` / `watchdog.stall.report` 两个 op 交换
结构化数据（NATS 信封与 proto 见 [operations.md](operations.md) §2）。

## 2. 四个模式（`ProactiveMode`，`proactive.rs:28-85`）

| 模式 | 求值 | 可记录/展示提案 | 已批准提案可自动执行 |
| --- | --- | --- | --- |
| `off`（**默认**） | 否 | 否 | 否 |
| `observe` | 是 | 否（记 `observed`，不可作为待办） | 否 |
| `propose` | 是 | 是（等人批准） | 否 |
| `execute` | 是 | 是 | 是 |

`from_str_lossy` 把 `off`/`disabled`/空串都归到 `off`，其余不认识的返回 `None`
（`proactive.rs:61-69`）——**不猜**。Python 侧的 `parse_proactive_mode` 遇到未知值回退 `off` 并打
warning（`python/ultimate_coders/proactive/modes.py`），原则写在注释里：*A typo must never turn
autonomy on* ——操作者必须察觉到功能**没**开，而不是它悄悄开了。

`observe` 是唯一可以「不开审批就打开」的模式：它是有测量意义的档位（记录建议但不呈现、不执行）。

## 3. 提案：有证据才算数

`ProactiveProposal`（`proactive.rs:168-213`）关键字段：

| 字段 | 作用 |
| --- | --- |
| `kind` | `discovered_followup` / `unblock_stalled` / `fix_failing_ci` / `maintenance_debt` / `investigate_alert` |
| `title` / `rationale` | 给人读的理由 |
| `confidence` | 排序提示，**不是概率、不授予执行**（没有基于它的阈值） |
| `evidence: Vec<String>` | 证据 id，例如 `ci:run-42`、`stall:task-1:2`、`alert:7`、`followup:<id>`、`debt:<id>`；**空 = 拒绝** |
| `proposed_request` / `project_id` / `estimated_tokens` | 若批准会提交什么、预估多少 token |
| `mode_at_creation` | 创建时的模式，让审计能区分「仅观察」与「批准执行」 |
| `executed_case_id` | 执行后创建的 case id |

`ProposalStatus` 七值（`proactive.rs:115-158`）：`observed`、`proposed`、`approved`、`rejected`、
`executed`、`expired`、`failed`。`is_open()` 只含 `proposed|approved`；终态是
`rejected|executed|expired|failed`。

Python `evaluator.py` 的规则是纯函数（无时钟、无随机、无网络、无模型调用），同一上下文产出同一批
提案，因此可以被重放与断言。`MAX_PROPOSALS_PER_RULE = 3` 限制单规则产出量；
**任务预算已耗尽（`BudgetVerdict.admitted is False`）时整体不评估**——「没钱了还建议更多活」正是预算
台账要防的事。可选注入的 `judge` **只能细化 confidence**（返回一个数字），不能返回提案；它抛异常或
返回垃圾时规则置信度保持不变。

## 4. 准入：两道门，顺序固定

`ProactiveGateState::admit_proposal`（`proactive.rs:356-384`）——**记录**提案时：

```
1. mode.evaluates()?             否 → deny proactive_mode_disallows(action="evaluate")
2. evidence 非空?                空 → deny proactive_mode_disallows(action="propose_without_evidence")
3. 开放提案数 < max_open?         否 → deny budget_exhausted(detail="open proactive proposals at capacity (n/m)")
```

`ProactiveGateState::admit_execution`（`proactive.rs:386-415`）——**执行**已批准提案时：

```
1. always_require_approval 且 status != approved?   是 → deny proactive_mode_disallows(action="execution_requires_approval")
2. mode.allows_execution()?                          否 → deny proactive_mode_disallows(action="execute")
3. status == approved?                               否 → deny proactive_mode_disallows(action="status_<状态>")
4. 有 max_tokens_per_action 且 estimated_tokens 超限 → deny budget_exhausted
```

两点设计意图：

- 第 1 条**排在最前**，是纵深防御：环境变量打错（本该 `observe` 却写成 `execute`）时，
  `always_require_approval` 仍能逼出一次人工批准；
- 前三条的 deny code 都是 `proactive_mode_disallows`，靠 `action` 细节区分「模式不对」「没证据」
  「状态不对」。审计时**必须读 detail，不能只看 code**。

`ProactiveGateState` 默认：`mode = off`、`max_open_proposals = 5`（`set_mode` 时夹到 ≥1）、
`max_tokens_per_action = None`、`always_require_approval = false`
（`proactive.rs:324-354`；`crates/uc-engine/src/governance/proactive.rs:141-164`）。
状态持久化在 `proactive` 命名空间的 `current` 键，重启后模式不回退（测试 `mode_change_persists`）。

## 5. 审计：允许和拒绝都留痕

`ProactiveAuditEntry`（`proactive.rs:418-472`）字段：`at`、`mode`、`action`、`proposal_id`、
`case_id`、`allowed`、`reason`（= deny code）、`detail`（deny reason 的 Debug 形态）。

`action` 的取值约定：`admit_proposal` / `admit_execution` / `decision` / `watchdog`。

`ProactiveGate::submit` 与 `admit_execution` 都**先写审计再返回**，包括拒绝路径
（`crates/uc-engine/src/governance/proactive.rs:171-254`）。
引擎侧 `off_mode_refuses_and_audits`、`evidence_free_proposal_is_refused`。

一个细节值得记录：拒绝时提案**仍会被存下来**（`Observe` 模式下状态置为 `observed`），条件是
`mode.evaluates()` 为真。理由写在注释里——让之后的审计能区分「我们看见了但没说」和「我们压根没看见」。

HTTP 面（`python/ultimate_coders/dashboard/proactive_api.py`）刻意**不重复**这条规则：
空的 `evidence` 会被照原样转发，由网关拒绝并回 `deny_code`/`deny_detail`。理由：本地也判一次会让两处
规则漂移，而投影层**不允许与控制面意见不一致**。被拒绝的提案返回 `200` + `accepted: false`
——请求成功了，答案是「不行」。

## 6. Watchdog：把「卡住了」变成有界处置

### 6.1 分工

- **检测**（`python/ultimate_coders/agent/progress.py`）：归一化错误签名、判断「同样的失败又发生了，
  文件也没变」。`UC_PROGRESS_STALL_LIMIT`（默认 2）在这一侧。
- **信号**（`python/ultimate_coders/proactive/watchdog_input.py` 的 `build_stall_signal`）：把检测器
  结论、尝试/重规划计数、已确认外部效果 id（`effect_ledger`）拼成
  `watchdog.stall.report` 载荷。**每个数字都是抄来的，不发明**。
- **处置**（Rust `WatchdogConfig::decide`）：决定 continue / retry / replan / escalate / stop。

### 6.2 五个 verdict（`WatchdogVerdict`，`proactive.rs:565-585`）

| verdict | 含义 |
| --- | --- |
| `continue` | 未达停滞阈值，正常继续 |
| `retry_same_contract { delay_ms }` | 同一契约重试一次（传输/瞬时失败类），不重新规划 |
| `replan { attempt, delay_ms, preserve_effects }` | 有界重规划；`preserve_effects` 列出已确认的外部效果，新计划**不得重复** |
| `escalate_needs_human { reason }` | 重规划预算耗尽 → 交给人 |
| `stop { reason }` | 尝试预算耗尽 → 停 |

### 6.3 判定顺序（`decide`，`proactive.rs:595-650`）

```
1. attempts_used >= max_attempts        → Stop（尝试预算先于停滞判断：没预算就没什么可重规划）
2. no_progress_count < stall_limit      → Continue
3. replans_remaining == 0              → EscalateNeedsHuman（不循环）
4. 否则                                 → Replan{ attempt = replans_used+1,
                                                   delay_ms = min(base << (attempt-1), max_backoff),
                                                   preserve_effects = 已确认效果 id }
```

`decide_with_retry`（`proactive.rs:652-671`）在 1–4 之前插一格：**首次**停滞且尝试/重规划预算都还有，
先给一次 `retry_same_contract`，因为重规划要花模型调用，而一次重试只花一次派发
（引擎测试 `first_stall_retries_then_replans_then_escalates`）。

### 6.4 边界参数（`WatchdogConfig` 默认值，`proactive.rs:517-563`）

| 字段 | 默认 | 镜像的环境变量 |
| --- | --- | --- |
| `stall_limit` | 2 | `UC_PROGRESS_STALL_LIMIT` |
| `max_replans` | 2 | `UC_WATCHDOG_MAX_REPLANS` |
| `max_attempts` | 5 | `UC_WATCHDOG_MAX_ATTEMPTS` |
| `base_backoff_ms` | 1000 | — |
| `max_backoff_ms` | 60000 | — |

Python 侧读同样三个变量（`watchdog_input.py`），语义钉死为「不可解析 → 用默认值，小于 1 夹到 1」，
两侧因此对「有界」的理解一致。

### 6.5 「重规划不得重复副作用」

`WatchdogSignal.confirmed_effect_ids` → `Replan.preserve_effects`（`proactive.rs:490-500`、
`proactive.rs:641-649`）；Python `apply_verdict` 会**拒绝**缺少任一已确认效果 id 的 replan
（`watchdog_input.py`）。测试：`watchdog_replan_preserves_confirmed_effects`、
`replan_preserves_confirmed_effects_for_audit`、`tests/python/test_proactive_watchdog.py`。

`effect_ledger` 里状态为 `UNKNOWN` 的效果**必须先对账**，模块明确说「此处不猜」。

## 7. 审计与历史

`Watchdog::decide` 每次判定都写一条 `WatchdogRecord`（`verdict` 的 wire token、`reason`、
`no_progress_count`、`replans_used`、`attempts_used`、`preserve_effects`、`at`），
key 为 `"{task_id}:{now_ms}"`，落在 `watchdog_decision` 命名空间
（`crates/uc-engine/src/governance/watchdog.rs:97-122`）。`history(task_id, limit)` 返回该任务最近的判定
（新→旧）。

这条记录回答的是运维最常问的问题：**「当时为什么重规划了？」**——答案在 `no_progress_count` 与
剩余预算里，而不是「模型觉得该重规划」。

## 8. HTTP 面（`python/ultimate_coders/dashboard/proactive_api.py`）

| 端点 | 对应 op |
| --- | --- |
| `GET /api/proactive/state` | `proactive.state.get` |
| `POST /api/proactive/mode` | `proactive.mode.set` |
| `GET /api/proactive/proposals` | `proactive.proposal.list` |
| `POST /api/proactive/proposals` | `proactive.proposal.submit` |
| `POST /api/proactive/proposals/{proposal_id}/decide` | `proactive.proposal.decide` |
| `GET /api/proactive/audit` | `proactive.audit.list` |
| `POST /api/watchdog/stall` | `watchdog.stall.report` |

约束：**传输，不是策略引擎**。请求只做形状校验（类型、token、范围），语义拒绝归网关；
网关不可达 → `503 governance_unavailable`，绝不伪造空状态（空提案列表会被读成「没有待审的东西」）；
鉴权由宿主注入（`build_router(auth=...)`），模块级 `router` 无鉴权、只能挂在已经有鉴权的宿主上。
principal 缺省为 `user`/`dashboard`，那是**归属默认**，不是授权。

测试：`tests/python/test_dashboard_proactive_api.py`、`tests/python/test_proactive_modes.py`、
`tests/python/test_proactive_evaluator.py`、`tests/python/test_proactive_watchdog.py`。

## 9. 验收：怎么手工验

1. `cargo test -p uc-types governance::proactive`
   （`default_mode_is_off_and_evaluates_nothing`、`observe_records_but_never_proposes_or_executes`、
   `propose_allows_approval_but_not_autonomous_execution`、
   `execute_mode_runs_only_approved_proposals_within_budget`、
   `proposal_without_evidence_is_refused`、`open_proposal_capacity_is_bounded`、
   `audit_entry_always_records_deny_reason`、`watchdog_continues_below_the_stall_threshold`、
   `watchdog_replans_are_bounded_then_escalate`、`watchdog_replan_preserves_confirmed_effects`、
   `watchdog_stops_when_attempt_budget_is_spent`、`watchdog_retries_once_before_replanning`）。
2. `cargo test -p uc-engine governance::proactive` 与 `governance::watchdog`
   （`off_mode_refuses_and_audits`、`evidence_free_proposal_is_refused`、
   `propose_mode_requires_approval_and_refuses_execution`、`mode_change_persists`、
   `first_stall_retries_then_replans_then_escalates`、`exhausted_attempts_stop_instead_of_looping`）。
3. `python -m pytest tests/python/test_proactive_evaluator.py tests/python/test_proactive_modes.py tests/python/test_proactive_watchdog.py tests/python/test_dashboard_proactive_api.py`。
4. 端到端（手工）：
   - 默认启动后 `GET /api/proactive/state` 必须是 `mode: off`；
   - 把 `UC_PROACTIVE_MODE` 设成拼错的值（如 `observ`），进程日志必须出现 warning，且状态仍为 `off`；
   - 提交空 `evidence` 提案 → `accepted=false`，`deny_code=proactive_mode_disallows`
     且 detail 里出现 `propose_without_evidence`；随后 `GET /api/proactive/audit` 必须能查到这条拒绝；
   - 对一个停滞任务连续 `POST /api/watchdog/stall`（`replans_used` 递增），第 3 次必须变成
     `escalate_needs_human`，第 5 次尝试后必须 `stop`。

## 10. 未实现 / 有意留空

| 项 | 现状 | 原因 |
| --- | --- | --- |
| 「批准后自动开 case」 | 未实现。`ProactiveProposal::mark_executed(case_id)` 只**记录**创建出来的 case id，创建动作由调用方（gRPC 服务层）完成 | 创建 case 需要签发 mandate（PR02/PR03），把它藏在提案状态机里会绕过授权链 |
| 提案过期清扫 | `ProposalStatus::Expired` 已定义，但没有清扫任务把它写进去 | 过期策略（多久算久）属于团队约定；没有清扫时提案停在 `proposed`，是可见状态 |
| 模型/LLM 直接产出提案 | 未实现。`judge` 只能细化 confidence，且失败不影响评估 | 不可审计的建议正是本 PR 要防的失败模式 |
| 自主执行的完整链路 | `admit_execution` 已就绪，但执行入口（谁在批准后真的提交工作）未接线 | 与 `uc-grpc` 并行改动一起落地；在此之前 `execute` 模式只是「允许」，不会自己动 |
| 重规划之后的执行 | verdict 已接线（见 §「在既有循环里」），但 `replan` 之后的**新计划**仍由网关/操作者决定：Python worker 只停止重试并上报 verdict，不自己改计划 | 改计划是网关的决策面，worker 自己重写计划就是第二套 planner |
| 预算台账（token 记账） | 未实现于本层：`max_tokens_per_action` 只比较**预估**值，`estimated_tokens` 是每 kind 的固定先验，不是预留 | 真实预算台账在执行面的 `effect_ledger`/预算模块；这里只做「明显超限就不批」 |

## 在既有循环里：watchdog 的实际接线

verdict 不再是「一个没人调用的库」。接线只有一处，且不新增调度器：

```
ProgressTracker（既有）→ stall_signal（既有）→ Orchestrator._apply_result（既有）
      └─> _maybe_stall_agent_redispatch_governed（新）
              └─> watchdog.stall.report  →  Rust Watchdog::decide（权威 staircase）
                      └─> apply_verdict  →  按 verdict 决定「还重试不重试」
```

| verdict | worker/orchestrator 动作 | 为什么 |
| --- | --- | --- |
| `continue` / `retry_same_contract` | 交给**既有**本地策略（是否换 agent），但 `attempts_remaining == 0` 时不再重试 | 阶梯预算归网关，本地策略只决定「换不换执行者」 |
| `replan` | 停止重试并上报 verdict，**不改计划** | 改计划是网关的决策面；worker 自己重写计划就是第二套 planner |
| `escalate_needs_human` / `stop` | 停止重试（`retryable = False`） | 网关已在服务端把升级投影成 `needs_human` 注意力项，worker 只需照做 |
| verdict 不可读（契约违约） | 按 `escalate` 处理 | 把畸形答复当「继续」会让它一直转下去 |
| 桥不可达 / 未接客户端 | **回到接线前的行为**（本地策略原样运行） | 桥故障不该冻结恢复；但也不能假装收到了 verdict |

两条实现细节：verdict 会被写回 `stall_signal`（`verdict` / `verdict_source=gateway`），随失败事件一起
发布，所以「是谁决定的」不用翻日志；凭空构造的 verdict 不会出现在事件里——没收到就不写。

开关与失败模式：`UC_WATCHDOG_BRIDGE=off` 显式关闭；worker 没有 NATS 连接或治理平面不存在时，请求失败被
`consult_watchdog` 降级为 `None`（一次 warn 日志），本地策略继续工作。

测试：`tests/python/test_watchdog_bridge.py`（15 个）——payload 形状与可选字段省略、transport 失败降级、
未知 verdict 抛契约错误、`replan` 丢已确认副作用被拒、`escalate`/`replan`/预算耗尽各自停止、桥不可达时
原行为不变、畸形 verdict 不落到重试。
