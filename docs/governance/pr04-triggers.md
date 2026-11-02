# PR04 — Trigger 与 Subscription：外部事件只有一个门，且不带授权

代码：`crates/uc-types/src/governance/trigger.rs`（事件身份、去重键、订阅匹配）、
`crates/uc-engine/src/governance/trigger_inbox.rs`（幂等入库 + 扇出）；Python 侧
`python/ultimate_coders/trigger/{__init__,github_webhook,github_poller}.py`（验签、载荷规范化、
轮询对账）与 `python/ultimate_coders/dashboard/trigger_api.py`（HTTP 门）。
proto：`crates/uc-grpc/proto/engine.proto:1200-1293`（`TriggerService`）。

## 1. 一句话模型

收件箱是外部事件的**唯一入口**；每个事件在触碰任何持久状态之前，先被归约成一个**确定性去重键**；
匹配按**线程**而不是按「哪个 webhook 先到」。触发器**只表达「发生了什么」，不表达「可以做什么」**。

## 2. 事件身份

### 2.1 枚举

`SourceKind` 7 种（`trigger.rs:24-62`）：`github` / `ci` / `lark`（别名 `feishu`）/ `dashboard` /
`api` / `cron` / `manual`。

`TriggerKind` 13 种（`trigger.rs:64-135`）：`issue_opened`、`issue_comment`、
`pull_request_opened`、`pull_request_synchronize`、`pull_request_review`、`pull_request_comment`、
`push`、`check_run`、`workflow_run`、`message`、`cron_tick`、`api_call`、`manual`。

`is_thread_update()`（`trigger.rs:121-134`）标出「这是既有线程上的进展，而不是新工作」的 7 种：
`issue_comment`、`pull_request_synchronize`、`pull_request_review`、`pull_request_comment`、
`check_run`、`workflow_run`、`push`。这组 kind 应当挂到既有 case 的订阅上，而不是开新 case。

### 2.2 去重键（核心）

```
dedup_key = sha256("{source}|{kind}|{external_id}|{payload_digest}")
```

（`TriggerEvent::dedup_key`，`trigger.rs:227-245`。`payload_digest` 由
`TriggerEvent::new` 取 `sha256(payload 的 JSON 序列化)`，`trigger.rs:184-205`。）

三个刻意的设计点，都有反例支撑：

| 设计 | 反例（如果不这样做） |
| --- | --- |
| **不含接收时间** | GitHub 的 `check_run` 完成事件与随后的轮询会拿到两个不同时间戳，永远无法收敛 |
| **`external_id` 用源原生身份**（issue/comment/review/check-run id、被推的 commit），**绝不用 delivery id** | delivery id 只存在于 webhook 路径，轮询的同一事件没有它，键永远对不上 |
| **无法给稳定 id 时混入 payload 摘要** | 两个真正相同、且源都不给 id 的载荷会被判为同一事件——这是保守的正确读法 |

> **契约与实现的一处不一致（需在 gRPC 接线时收敛）**：`engine.proto` 里
> `IngestTriggerRequest.delivery_id` 的注释写着「存在时折进去重键」（`engine.proto:1221-1223`），
> 但 Rust 的 `TriggerEvent` **没有** `delivery_id` 字段，`dedup_key()` 也不使用它
> （`trigger.rs:157-245`），Python 侧更明确禁止把 delivery id 当身份。三者中两票支持「不进键」，
> 因此本节以代码为准：**delivery id 只用于追踪，不参与身份**。接线时应改注释，而不是改实现——
> 改实现会让轮询孪生事件永远无法去重。

`thread_key_for(repo, number)` = `"{repo}#{number}"`（`trigger.rs:222-225`）；
Python 侧同名规则见 `normalize_thread_key`（`github_webhook.py`），并且它**拒绝**没有编号线程的
kind（否则 `push` 会被压成 `owner/repo#0`，把多个 commit 折叠成一条线程）。

测试（`crates/uc-types/src/governance/trigger.rs` 测试模块）：`redelivery_of_the_same_webhook_dedupes`、
`different_events_on_the_same_thread_have_different_keys`、`thread_key_format_is_stable`、
`thread_updates_are_distinguished_from_new_work`。

## 3. 入库：幂等 + 扇出

`TriggerInbox::ingest`（`crates/uc-engine/src/governance/trigger_inbox.rs:72-141`）：

```
1. events.put(dedup_key, event, expected_version = 0)    ← create-only CAS
      冲突 → 返回 IngestOutcome::duplicate(accepted=false, duplicate=true, matched_cases=[])，结束
2. 扫订阅（上限 1000）：inactive 跳过、source 不匹配跳过、filter 不匹配跳过
3. 命中的 case_id 去重后放进 matched_cases；对每个订阅 record_event（计数、last_event_at）
4. matched_cases 为空时给出 unmatched_reason
```

两个值得注意的实现选择：

- **重复投递不扇出**。第二份 webhook 不会让 case 再收一次事件，所以「CI 成功」这类事件不会因为
  GitHub 重投而触发两轮工作。测试：`redelivery_is_reported_as_duplicate_and_matches_nothing`。
- **订阅计数用 CAS 更新，但输掉竞争无所谓**（`trigger_inbox.rs:112-123`）：计数是观测值，不是
  路由依据；为它重试会放大热点。订阅更新时 `upsert_subscription` 会保留既有计数
  （`trigger_inbox.rs:143-182`，测试 `subscription_update_preserves_delivery_statistics`）。

`unmatched_reason` 只有两个取值（`trigger_inbox.rs:125-131`）：`thread_has_no_subscription`
（事件有线程键但没人订阅）与 `no_subscription`（连线程键都没有）。

> **关键边界**：「没有任何订阅命中时是否自动开一个 case」**不在收件箱里做**
> （`trigger_inbox.rs:74-76` 注释）。理由：那是一个策略决定（一条没打标签的评论，值得开启一整个
> case 吗？），所以由调用方根据 `unmatched_reason` 决定。proto 里对应
> `IngestTriggerRequest.open_case_if_unmatched` / `open_case_project_id`
> （`engine.proto:1227-1230`），执行者是服务层，不是存储层。

## 4. 订阅与匹配

`Subscription { id, case_id, source, filter, active, created_at, last_event_at, event_count }`
（`trigger.rs:338-379`）。`event_count` 的作用是让提问者能区分「还没有事件」和「订阅坏了」。

`SubscriptionFilter::matches`（`trigger.rs:284-316`）逐条短路：

| 维度 | 规则 |
| --- | --- |
| `repo` | 设置了就必须完全相等 |
| `kinds` | 空 = 该 source 的所有 kind；否则必须包含事件 kind |
| `thread_key` | 设置了就必须完全相等（线程订阅的标准形态） |
| `labels` | 非空时从事件的 `payload.labels` 数组读，**任一**期望标签命中即可 |

测试：`thread_subscription_matches_only_its_thread`、`kind_and_repo_filters_compose`、
`label_filter_reads_payload_labels`、`subscription_counts_events`、
`inactive_subscriptions_do_not_match`、`events_are_retained_for_audit`。

## 5. GitHub：webhook 与轮询必须收敛

### 5.1 webhook 侧（`python/ultimate_coders/trigger/github_webhook.py`）

纯函数、无 I/O，负责三件事：

1. **验签** `verify_signature(secret, body, header)`：`X-Hub-Signature-256`，`hmac.compare_digest`
   定长比较；**空 secret、缺失或过短 header 一律拒绝**（不是放行）。测试见
   `tests/python/test_github_trigger_webhook.py`。
2. **把 GitHub 载荷压成规范事件**：`event_from_body(...)`，只保留身份与状态字段
   （`payload` 是**投影**，不是原始 body）——因为去重摘要取的是「我们转发什么」，而 URL、时间戳、
   渲染后的 HTML 在 webhook body 与 events-API 条目之间天然不同。
3. **规范化线格式**：`canonical_payload_json( payload )` 用 `sort_keys=True` +
   `separators=(",",":")`，让 Python 侧 dict 顺序不能影响摘要；`ingest_payload(...)` 是
   **唯一**的 `trigger.ingest` 载荷构造函数，webhook 路径与轮询路径都必须经过它。

上限与诚实边界：`MAX_PAYLOAD_BYTES` 1 MiB；`SUPPORTED_EVENTS` 之外的 event type 解析为 `None`
（忽略），**没有映射的 kind 表现为缺口而不是被误标**；完整 delivery body 不保留，需要审计的调用方
必须自行在解析前落盘。

### 5.2 轮询侧（`python/ultimate_coders/trigger/github_poller.py`）

`GitHubReconciler` 是「webhook 至少一次、且可能全丢」的兜底对账器。它的核心不变量是：
**被轮询到的事件，在去重键涉及的每个字段上，必须与它的 webhook 孪生完全相同**。因此它
**复用** `event_from_body` 而不是重新实现一遍映射，并且**永不**发明 `delivery_id`。

| 参数 | 值 | 说明 |
| --- | --- | --- |
| `CURSOR_NAMESPACE` | `trigger_cursor` | 游标按仓库持久化 |
| `DEFAULT_MAX_ITEMS` | 50 | 每个仓库每次轮询上限，**最旧的先处理**（积压会被消化而不是静默丢弃） |
| `DEFAULT_BACKOFF_BASE_SECONDS` / `MAX` | 5 → 300 | 抓取失败记指数退避，退避期间**不调用** fetch |
| 传输 | 注入 | 本模块不引入 HTTP 依赖；条件请求（etag/if-modified-since）由调用方负责 |

游标按 GitHub event id 单调前进；不支持的事件类型在游标越过它们**之后**才被跳过，所以「以后再加映射」
不会回填历史（events API 本身也只提供近期事件）。

收敛这条性质由
`tests/python/test_github_reconciler.py::test_polled_event_and_its_webhook_twin_share_the_dedup_key`
钉住：该测试**在本地重算一遍** sha256 去重键，证明「轮询事件」与「它的 webhook 孪生」归约出的输入
完全相同。它刻意不调用 Rust 实现——要证明的是「Python 转发出去的东西」正确，而不是「Rust 算得对」。
同一个文件还钉住了有界性：最旧优先、`max_items` 上限、游标按仓库隔离、失败退避与恢复清零。

> **未实现**：非 GitHub 的签名方案。`SourceKind` 有 `ci` / `lark`，但仓库里只有 GitHub 的
> `sha256=` HMAC 校验；各 provider 算法不同，猜一个比说「不支持」更危险。

## 6. HTTP 门（`python/ultimate_coders/dashboard/trigger_api.py`）

| 端点 | 认证 | 行为 |
| --- | --- | --- |
| `POST /api/triggers/github` | `UC_GITHUB_WEBHOOK_SECRET` 验签；**未配置 = 503 fail-closed** | 先对原始 body 验签，再解析，再转发 `trigger.ingest` |
| `POST /api/triggers/ingest` | 设置了 `UC_DASHBOARD_TOKEN` 则要求 Bearer，未设置则不校验（沿用 dashboard 既有约定，由外层部署守门） | 运维/自动化入口，可带 `open_case_if_unmatched` |
| `GET /api/triggers/subscriptions` | 只读 | 转发 `trigger.subscription.list` |

三条行为约束，都是为「不撒谎」：

- **绝不本地伪造 ingest 结果**：本地回一个 `accepted` 是网关无法撤销的谎言，所以网关不可达就是 503；
- **不重复实现去重**：签名通过后照原样转发，去重仍由网关按同一 dedup key 判定；
- **principal 是调用方（service/dashboard），不是事件**（`ingest_payload` 注释：*a trigger holds
  no authority*）。

测试：`tests/python/test_dashboard_trigger_api.py`。

## 7. 为什么触发器不带授权

三重落地：

1. **数据结构上不给字段**：`TriggerEvent` 没有「申请某个 scope」「预算」「工具」这类字段
   （`trigger.rs:157-182`）；`payload` 原样保存供审计与求值，但注释明确「nothing in it is trusted for
   authorization decisions」。
2. **权限模型上不给可能**：`PrincipalKind::ExternalTrigger` 只能持有非授权类权限，见
   [pr03-policy.md](pr03-policy.md) §2；事件的调用方身份是验签的 service，不是事件自身。gRPC 层还
   直接拒绝调用方声明 `external_trigger` / `anonymous`
   （`crates/uc-grpc/src/governance/policy_interceptor.rs:19-26`），所以「以事件身份调网关」这条路径
   在传输边界上就不存在。
3. **流程上不给路径**：收件箱只扇出到既有订阅，不创建 case、不签发 mandate。要「凭一条评论开工」，
   必须有一个已授权的 principal 显式创建 case 并签发 mandate——触发器只能让这件事**被注意到**。

因此在 webhook 载荷完全由攻击者控制的前提下，`不越权` 仍然成立：最坏情况是「有人能让你多看一眼」，
而不是「有人能拿到权限」。

## 8. 验收：怎么手工验

1. `cargo test -p uc-types governance::trigger`（去重键、线程键、过滤器组合、计数）。
2. `cargo test -p uc-engine governance::trigger_inbox`
   （`redelivery_is_reported_as_duplicate_and_matches_nothing`、
   `three_events_on_the_same_thread_all_reach_the_same_case`、
   `unmatched_event_reports_a_reason`、`inactive_subscriptions_do_not_match`）。
3. `python -m pytest tests/python/test_github_trigger_webhook.py tests/python/test_github_reconciler.py tests/python/test_dashboard_trigger_api.py`。
4. 端到端（手工）：
   - 不设 `UC_GITHUB_WEBHOOK_SECRET` 时 `POST /api/triggers/github` 必须 503；
   - 设了 secret 后，用同一 delivery 重放两次 → 第一次 `accepted=true, duplicate=false`，
     第二次 `duplicate=true` 且 `matched_cases` 为空；
   - 同一 PR 的 `issue_comment`、`pull_request_synchronize`、`check_run` 三条事件，
     `thread_key` 必须相同，并且命中同一个 case。

## 9. 未实现 / 有意留空

| 项 | 现状 | 原因 |
| --- | --- | --- |
| 非 GitHub provider 的验签 | 未实现（见 §5.2） | 算法因 provider 而异；不做比做错好 |
| 「无订阅时自动开 case」的默认策略 | 存储层不做，由服务层按 `unmatched_reason` 决定 | 这是策略问题，不是数据问题 |
| 事件正文归档 | 未实现（只存规范化 payload） | 完整 body 可能含敏感内容；需要审计的调用方应在解析前自行落盘 |
| 订阅的规则引擎（例如「只有 labelled:bug 才开 case」） | 只支持 repo/kind/thread/label 四个维度 | 更复杂的规则属于策略层，硬编码进订阅会把策略藏进数据 |
| 轮询的 HTTP 客户端与条件请求 | 未实现（传输注入） | 本模块不引入 HTTP 依赖；由部署侧提供 |
