# PR05 — Attention：等待不占槽位，只有人类决策打断人类

代码：`crates/uc-types/src/governance/attention.rs`（种类分类、排序键、容量算术，纯函数）、
`crates/uc-engine/src/governance/attention.rs`（`AttentionBoard`：持久化投影）、
`python/ultimate_coders/attention/{model,capacity}.py`（Python 投影与人类预留策略）、
`python/ultimate_coders/dashboard/attention_api.py`（HTTP 面）。
proto：`crates/uc-grpc/proto/engine.proto:1295-1382`（`AttentionService` + `CapacityProto`）。

## 1. 「少打扰」的两半

「少打扰」不是「少发通知」，而是两条可判定的规则：

1. **等待中的 case 不占执行资源**——CI 跑一小时，不应该占掉一个 Worker 槽位；
2. **只有人类决定才打断人类**——`blocked` 上台账但不 page 人，因为按定义它不是「某个决定能立刻解开
   的东西」。

两条都落在 `AttentionKind` 上，而不是散落在调用点的约定。

## 2. 四种注意力（`attention.rs:19-94`）

| kind | 含义 | 占 Worker 槽位 | 预留人类容量 | 立刻打断人 | 出现在工作清单 |
| --- | --- | --- | --- | --- | --- |
| `waiting_event` | 等外部事件（CI、PR review、webhook） | 否 | 否 | 否 | 是 |
| `needs_human` | 需要人类决定（需求歧义、审批、合并冲突裁决） | 否 | **是** | **是** | 是 |
| `blocked` | 卡在既非事件也非人工决定的先决条件（依赖失败、预算耗尽、授权被吊销） | 否 | 否 | 否 | 是 |
| `verifying` | 执行完成、独立验证中 | 否 | 否 | 否 | 否 |

四个谓词都是**总函数**（穷举 match，没有 default）：

- `holds_worker_slot()` 恒为 `false`，理由写在注释里：注意力状态的定义就是「这个 case 没有在执行」；
  如果它在执行，状态会是 `active`，而 `active` 不是注意力状态。
- `reserves_human_slot()` 只对 `needs_human` 为真；`waiting_event` 明确为假——为一个可能停几小时的
  case 预留容量，正是计划里禁止的浪费。
- `requires_human_now()` 只对 `needs_human` 为真。
- `is_visible_in_worklist()` 除 `verifying` 外都为真（验证是执行侧的事，不需要人来读）。

测试：`waiting_for_ci_never_holds_a_worker_slot`、`only_needs_human_interrupts_a_human`、
`attention_kind_tokens_round_trip`。

Python 侧把同一张表做成**按 kind 建键的字典**（`python/ultimate_coders/attention/model.py`）：
新增一个 kind 却忘了分类会直接 `KeyError`，而不是静默继承某个默认值——这比 Rust 的穷举 match
更啰嗦，但同样 fail-closed。

## 3. 一个 case 只有一个开放项

`AttentionBoard::raise`（`crates/uc-engine/src/governance/attention.rs:67-111`）：

```
1. 找该 case 当前的开放项（按 since 取最新）
2. 若存在且 id 不同 → 用 Automatic { detail: "superseded by <新id>" } 关闭它
3. 以 create-only CAS 写入新项
     冲突（同 id 重复 raise）→ 原地更新，并保留原 since（年龄不被重置）
```

这条规则的意义：没有它，工作清单会被同一个阻塞源的重复项填满（每次重试都 raise 一次）。
「保留 `since`」同样重要——否则「刷新一次就变新」，按年龄排序会失去意义。

`resolve` 是**幂等**的：已经解决的项再解决一次，保留第一次的结论（`attention.rs:113-138`，
类型层同样在 `AttentionItem::resolve` 里做了短路，`attention.rs:208-214`）。
测试：`raising_a_second_item_supersedes_the_first`、`resolving_is_idempotent`、
`unknown_item_resolution_is_an_error_not_a_silent_noop`。

模块文档把这条边界说得最清楚：**项是投影，不是锁**。调度器不依赖某个项是否存在；项丢了可以重建。
这是 PR05 里最容易被误用的一点——不要把「有个 needs_human 项」当成「这个 case 被挂起了」。

## 4. 工作清单的确定性排序

`AttentionItem::worklist_order_key()`（`attention.rs:216-227`）返回四元组：

```
(是否人类决定优先(0/1), Reverse(priority), since_millis, case_id)
```

排序结果：**先人类决定，再优先级高的，再更老的，最后按 case_id 兜底**。
最后一个分量是必要的：没有它，同优先级同毫秒的两项顺序不稳定，Dashboard 每次刷新会抖动。

`priority` 来自 case（`WorkCase.priority`，被 `MAX_PRIORITY = 9` 夹住，见
[pr02-workcase.md](pr02-workcase.md)），所以触发器不能靠自封高优先级把别人的活饿死。

Python 侧 `worklist_order(...)` 与 `attention_api.py` 都**重新应用**同一顺序，而不是信任网关返回的
次序（`attention_api.py` 模块文档：*The worklist order is re-applied here rather than trusted from the
gateway*），这样两个客户端同时请求也看到同一份清单。测试：
`worklist_puts_human_decisions_first_then_priority`（类型层与引擎层各一份）。

一个诚实的取舍：`since` 解析失败的项被**钳到 Unix epoch**（Python `model.py`），于是它排到最老的位置
——而不是被丢弃。这是「宁可显眼，不可隐藏」：解析失败会以排序异常的形式被看见。

## 5. 人类优先预留（`UC_ATTENTION_HUMAN_RESERVE`）

策略在 `python/ultimate_coders/attention/capacity.py`，默认 `1`：

```
无开放 needs_human 项            → worker_slots_reserved_human = 0
有 ≥1 个开放 needs_human 项      → reserved = max(UC_ATTENTION_HUMAN_RESERVE, 开放项数)，上限为槽位总数
```

第一行是重点：**预留只在真的有人在等时才武装**。一个常量式的「永远留 1 个槽」会让每个空闲时刻都
浪费 1/N 的算力；而「等有人问再腾地方」来不及——决策做出到开始执行会排到长任务后面。
Rust 侧对应 `AttentionBoard::capacity(..., reserved_human)`，由调用方传入并夹到
`reserved_human.min(worker_slots_total)`（`crates/uc-engine/src/governance/attention.rs:198-227`）——
「留多少余量」是运维设定，不是项的属性，所以不写死在板子里。

## 5.1 等待中的 case 由「事件到达」结束（`case_wake.rs`）

注意力项不是锁，`waiting_event` 也不是「等到天荒地老」。结束等待的只有一个触发点：
**订阅匹配到的 trigger 事件入库时**（`crates/uc-engine/src/governance/case_wake.rs::wake_matched_cases`，
由 `crates/uc-grpc/src/governance/trigger_service.rs` 的 `ingest` 调用）。

判定顺序与理由：

| case 当前状态 | 事件到达时 | 为什么 |
| --- | --- | --- |
| `waiting_event` | 转为 `active`，并把它开放的 `waiting_event` 注意力项以 `EventReceived{trigger_id}` 解决 | 它等的就是这个事件；不结束等待，注意力项永远没人能手动处理，容量投影也会把「早已结束的等待」继续算成省下的槽位 |
| `needs_human` | **不动** | 待决的是人的决定；一条 webhook 不能替人回答 |
| `blocked` | 不动 | blocked 不是「等事件」，解除原因不同（依赖/预算/授权），解除者也不同 |
| `verifying` | 不动 | run 已结束、验证在飞；事件不能把 case 拉回 `active` 绕过验证器 |
| 终态 | 不动 | 没有可唤醒的东西 |

两条关键设计判断：

1. **匹配即授权**。唤醒只看「这个 case 的某条订阅匹配了事件」，不解析 payload —— payload 里写什么都不能
   决定它唤醒谁。订阅的 `SubscriptionFilter` 已经声明了这个 case 关心的事件 kind，这是可审计的授权来源。
2. **失败不失败整个入库**。唤醒失败（store 抖动、case 被别处改了状态）只 `tracing::warn!` 并继续，因为事件
   本身已经持久化；把唤醒失败变成入库错误会让调用方重试，而重试会被收件箱当重复丢掉，反而永久丢失唤醒。

测试：`a_matched_event_wakes_the_case_and_resolves_its_attention`、
`a_needs_human_case_is_not_woken_by_an_incoming_event`、`a_duplicate_delivery_wakes_nothing`、
`a_waiting_case_without_an_attention_item_still_wakes`（注意力板是投影，丢了项也不能把 case 卡死）；
服务层接线由 `crates/uc-grpc/tests/governance_services.rs::an_arriving_event_wakes_a_waiting_case` 覆盖
（经 gRPC impl 入库，再用 `GetCase` / `ListAttention` 读回状态）。
## 6. 容量快照与「省下来的槽位」指标

`CapacitySnapshot`（`attention.rs:230-269`）九个字段：

| 字段 | 含义 |
| --- | --- |
| `worker_slots_total` / `worker_slots_busy` | 配置槽位 / 正在执行 |
| `worker_slots_reserved_human` | 为人类决策预留 |
| `waiting_event_cases` / `verifying_cases` | 停在 `waiting_event` / `verifying` 的 case 数（**不占槽**） |
| `needs_human_items` | 开放的人类决策项数（工作清单深度） |
| `blocked_cases` | 停在 `blocked` 的 case 数 |

三个派生量：

- `worker_slots_available() = total - busy - reserved`（**逐项 saturating**，永不为负）；
- `can_admit_worker() = available > 0`；
- `slots_saved_by_quiet_waiting() = waiting_event_cases + verifying_cases`。

最后这个就是「少打扰」的**可观测指标**：如果等待中的 case 各占一个槽，这些槽就没了。
它应当恒等于 `waiting_event_cases + verifying_cases`——这是一个可以拿来做断言/告警的等式
（测试 `capacity_reserves_human_headroom_and_reports_savings`、
引擎侧 `capacity_reports_quiet_waiting_savings_and_human_reserve`、
Python 侧 `tests/python/test_attention_capacity.py`）。

两条边界诚实说明（`capacity.py` 注释）：

- 超订（busy + reserved > total）时**只把 available 夹到 0**，`busy` 原样上报——隐藏超订就是隐藏调度
  bug；
- `*_cases` 数的是**不同的 case**（一个 case 两个开放项仍算一个停在等待的 case），
  `needs_human_items` 数的是**项**（人实际看到的工作清单深度）。

## 7. HTTP 面（`python/ultimate_coders/dashboard/attention_api.py`）

| 端点 | 对应 op | 说明 |
| --- | --- | --- |
| `GET /api/attention` | `attention.list` | 工作清单 + 容量；顺序在本地重排 |
| `POST /api/attention/{attention_id}/resolve` | `attention.resolve` | 记录人类决定；resolution 四值之一 |
| `GET /api/capacity` | `capacity.get` | 容量快照 |
| `GET /api/cases` / `GET /api/cases/{case_id}` | `case.list` / `case.get` | 只读 case 视图（case 由网关拥有，原样转发） |

行为约束：

- **网关不可达 = `503 {"error": "governance_unavailable"}`**，不是 500 traceback，也不是「空清单」；
- 只在网关侧维护状态，Dashboard 不缓存、不推断；
- `attention.resolve` **总是以 `user` principal 执行**——UI 不能借到 service/agent 的身份；
- 读端点不做 token 校验（它们只暴露治理元数据，不含授权），唯一的写端点遵循既有
  `UC_DASHBOARD_TOKEN` 约定。

测试：`tests/python/test_attention_model.py`、`tests/python/test_attention_capacity.py`、
`tests/python/test_dashboard_attention_api.py`。

## 8. 验收：怎么手工验

1. `cargo test -p uc-types governance::attention`
   （`waiting_for_ci_never_holds_a_worker_slot`、`only_needs_human_interrupts_a_human`、
   `resolution_is_idempotent`、`worklist_puts_human_decisions_first_then_priority`、
   `capacity_reserves_human_headroom_and_reports_savings`）。
2. `cargo test -p uc-engine governance::attention`
   （`raising_a_second_item_supersedes_the_first`、`resolving_is_idempotent`、
   `worklist_puts_human_first_then_priority`、`capacity_reports_quiet_waiting_savings_and_human_reserve`、
   `unknown_item_resolution_is_an_error_not_a_silent_noop`）。
3. `python -m pytest tests/python/test_attention_model.py tests/python/test_attention_capacity.py tests/python/test_dashboard_attention_api.py`。
4. 端到端（手工）：
   - 让一个 case 进 `waiting_event`，看 `GET /api/capacity`：`slots_saved_by_quiet_waiting` 必须
     增加 1，且 `worker_slots_available` **不变**（等待不吃槽）；
   - 同一个 case 连 raise 两次 `needs_human`：清单里只有一项，且第一项 `resolution` 变成
     `automatic: superseded by …`；
   - 设 `UC_ATTENTION_HUMAN_RESERVE=2`、开 1 个 `needs_human`：`worker_slots_reserved_human` 应为 2
     （`max(2, 1)`）；没有 `needs_human` 时应为 0。

## 9. 未实现 / 有意留空

| 项 | 现状 | 原因 |
| --- | --- | --- |
| 调度器真正消费 `CapacitySnapshot` | 未实现：`CapacitySnapshot` 的 `worker_slots_reserved_human` 由调用方传入，Rust 调度器（`WorkerRegistry` 的准入）尚未读取它 | 准入改造属执行面；本 PR 先把「应该预留多少」变成可计算的纯函数与可观测字段 |
| 通知/升级通道 | 未实现：`requires_human_now()` 只是谓词，仓库里没有 pager/webhook 推送 | 通知通道是部署策略（可能是 Lark、可能是邮件），治理层只保证「该不该打扰」有唯一答案 |
| 项的老化/超时自动升级 | 未实现 | 「等多久算太久」按团队而异；硬编码会把策略藏进代码 |
| Dashboard 侧的鉴权细化 | 读端点不校验 token（见 §7） | 它们只暴露治理元数据；写端点沿用既有 token 约定 |
| `verifying` 项的可见性 | 不进工作清单 | 验证是执行侧进展，人不需要读；需要排障时应查 case/run，而不是查注意力项 |
