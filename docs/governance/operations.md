# 治理层运维：环境变量、启动、验收与升级顺序

本文件只写**可执行**的部分：变量怎么配、默认值为什么安全、配错会坏在哪、上线按什么顺序。
设计动机见各 PR 文档（[README.md](README.md) 有索引）。
需要**真实集群**（Docker + NATS）的端到端验收步骤见 [e2e-runbook.md](e2e-runbook.md)，执行时该记录什么见 [e2e-checklist.md](e2e-checklist.md)；本文件里的验收都用单测/集成测试即可复现。
本地开发用 `docker/docker-compose.dev.yml`（`--profile engine --profile storage`）：它把 `UC_GOVERNANCE_BACKEND=postgres` / `UC_DATABASE_URL` / `UC_POLICY_BOOTSTRAP=dev` / `UC_OPERATOR_ID` / `UC_PROACTIVE_MODE=off` 一并注入网关容器；该 dev 快照**绝不能**用于共享环境。

## 0. 现状与文档边界

启动契约（`UC_GOVERNANCE_BACKEND` / `UC_DATABASE_URL` / `UC_POLICY_BOOTSTRAP` / `UC_OPERATOR_ID` / `UC_POLICY_GRANTS_JSON` / `UC_PROACTIVE_MODE`）由 `crates/uc-grpc-server/src/governance_boot.rs` 的 13 个测试固定：空环境**拒绝一切**、dev 快照仍受 principal-kind 天花板约束、显式 grants 带项目作用域、非法 grants 与拼错的权限都不放宽、**已持久化的快照优先于环境**（重启不会把策略重置成 dev）、未知 `UC_PROACTIVE_MODE` 不改模式、未知后端/缺 `UC_DATABASE_URL` 都退回内存并诚实报告非持久。这些测试注入环境查找函数而不是改进程变量，所以可并行、可重复。

崩溃恢复（`recover`）也有 5 个测试：发布中途崩溃的 outbox 条目会被重置为 `Pending` 并**只保留一条**（至少一次，绝不重复入队）；已 `delivered` 的条目不受影响；记录仍是 `received` 但发布丢失的 submission 会被重新排队，且恢复载荷带 `"recovered":true`；重复启动不会双入队；已 `applied` 的 submission 不会被当作丢失再发一次。

| 层 | 状态 |
| --- | --- |
| 治理类型与判定（`crates/uc-types/src/governance/`） | 已落地，带内联测试 （测试：`external_trigger_cannot_hold_authority_granting_permission`、`a_permit_is_single_use`） |
| 治理存储（`crates/uc-engine/src/governance/`） | 已落地：`RecordStore`（内存 + PostgreSQL）、提交/外发箱、case/mandate、policy、trigger、attention、proactive、watchdog （测试：`same_key_different_content_is_rejected_as_conflict`、`a_repeated_request_converges_instead_of_duplicating`） |
| gRPC 服务与 NATS 桥（`crates/uc-grpc/src/governance/`） | **已落地**：身份/授权闸口 `policy_interceptor.rs`、五个服务（`case_service.rs` / `trigger_service.rs` / `attention_service.rs` / `proactive_service.rs` / `approval_service.rs`）、NATS 请求/回复分发（30 个 op，`bridge.rs`）、`spawn_nats_bridge`（含 2s outbox 分发器 + 启动时 `outbox.recover`）、`GrpcServer::with_governance` 与 `into_governance_services` / `into_intercepted_governance_services` 接线。网关 bootstrap 见 `crates/uc-grpc-server/src/governance_boot.rs`。**契约以 `crates/uc-grpc/proto/engine.proto` 为权威**，错误码映射见 `crates/uc-grpc/src/governance/mod.rs` |
| Python 请求方（`python/ultimate_coders/governance_client.py`） | 已落地：NATS request/reply 客户端（submission / case / mandate / trigger / attention / capacity / proactive / watchdog / approval 各组 op） （测试：`tests/python/test_governance_client.py`） |
| Python 投影与 HTTP 面（`python/ultimate_coders/trigger`/`attention`/`proactive` 包 + `python/ultimate_coders/dashboard/trigger_api.py` / `attention_api.py` / `proactive_api.py` / `approval_api.py`；worker 侧 `python/ultimate_coders/agent/watchdog_bridge.py` / `proactive_bridge.py`） | 已落地，且已挂进 `python/ultimate_coders/dashboard/app.py`；watchdog 与 proactive 已在既有停滞接缝上接线 （测试：`tests/python/test_dashboard_approval_api.py`） |
| 审计导出（`GET /api/approvals/export.csv`） | 已落地：按 `offset` 翻页取全量审批历史，保留网关原始时间戳，中和表格公式，达到上界时用 `X-UC-Approvals-Truncated` 明示 （测试：`test_export_walks_the_gateway_pages_until_a_short_one`） |

接线已完整：网关启动即建立治理平面（存储 → 策略 → 崩溃恢复 → NATS 桥，见
`crates/uc-grpc-server/src/governance_boot.rs`）。单测覆盖的是进程内路径；真实验收需要
NATS + PostgreSQL（见 §4 手工验收）。

## 1. 环境变量

### 1.1 治理层变量

| 变量 | 取值 | 安全默认 | 作用 | 读取点 | 配错的后果 |
| --- | --- | --- | --- | --- | --- |
| `UC_GOVERNANCE_BACKEND` | `memory` \| `postgres` \| `pg` | `memory` | 选择治理记录存储后端 | `crates/uc-grpc-server/src/governance_boot.rs::choose_record_store`（未知值 → 内存 + warning，不静默当 postgres） | 生产上误留 `memory`：重启后提交记录、case、mandate、审计**全部消失**——注意 `Governance::in_memory()` 会如实报告 `is_durable() == false`（`crates/uc-engine/src/governance/mod.rs:100-110`，测试 `in_memory_governance_reports_itself_as_not_durable`），不会假装耐用 |
| `UC_DATABASE_URL` | PostgreSQL URL | 未设置 | 治理表 `governance_records` 与既有 task/graph 表共用同一库 | `crates/uc-engine/src/governance/record_store.rs:263`；既有用法见 `crates/uc-grpc-server/src/main.rs` | 设了 `postgres` 后端却缺这个变量：启动必须**明确失败**而不是静默退回内存（既有后端已按此处理，见 `main.rs` 里 `UC_TASK_BACKEND=postgres but UC_DATABASE_URL not set` 的 reason 写法）；连错库：治理数据与任务数据分裂，`case.run → task_id` 交叉引用查不到 |
| `UC_POLICY_BOOTSTRAP` | `dev` \| `none` | `none` | `dev` 装载 `PolicyEngine::development_snapshot()`：`user:<UC_OPERATOR_ID>`（默认 `operator`）持全部 15 个权限；`service:dashboard` 与 `user:dashboard` 只持 Dashboard 需要的 9 个（读、提交、case、trigger ingest、attention resolve、proactive propose/execute；**不含** `worker_scale` / `mandate_issue` / `mandate_revoke`） | `crates/uc-grpc-server/src/governance_boot.rs::bootstrap_policy`；快照定义见 `crates/uc-engine/src/governance/policy.rs::development_snapshot` | 生产误设 `dev`：**任何能提交请求的身份都拿到 `worker_scale` / `mandate_issue` / `merge_grant`**——相当于关掉 PR03；反之 `none` 且没装策略时是空快照，**一切拒绝**（测试 `empty_snapshot_denies_everything`），表现为「所有操作 401/403 类拒绝」 |
| `UC_PROACTIVE_MODE` | `off` \| `observe` \| `propose` \| `execute` | `off` | 自治上限；只表示「允许到哪一档」，不授予执行权 | Rust 读取点 `crates/uc-grpc-server/src/governance_boot.rs:232-240`（未知值 → 保持 `off` + warning）；Python 解析见 `python/ultimate_coders/proactive/modes.py` | 拼错 → 保持 `off`（安全方向，但功能「没开」，日志里有 warning）；误设 `execute` → 已批准提案可无第二次人工确认就开工（仍需 `proactive_execute` 权限、approved 状态、token 预估不超限） |
| `UC_WATCHDOG_BRIDGE` | `on` \| `off` | `on` | Worker 是否向网关询问停滞 verdict （`watchdog.stall.report`） | `python/ultimate_coders/agent/watchdog_bridge.py`、`nats_worker._watchdog_governance_client` | `off` 时保留接线前的本地重试策略；网关不可达时同样回落到本地策略（一次 warn，不会冻结恢复） |
| `UC_GITHUB_WEBHOOK_SECRET` | 任意字符串 | 未设置 | `POST /api/triggers/github` 的 HMAC-SHA256 验签密钥（`X-Hub-Signature-256`） | `python/ultimate_coders/dashboard/trigger_api.py`（`GITHUB_WEBHOOK_SECRET_ENV`） | **未设置时端点 fail-closed 返回 503**——这是有意为之，不是 bug；设错时所有 GitHub 投递验签失败、触发器静默不来（靠轮询对账兜底，但会延迟）；密钥泄露等于允许伪造事件（虽仍不授予权限，见 [pr04-triggers.md](pr04-triggers.md) §7） |
| `UC_ATTENTION_HUMAN_RESERVE` | 整数 ≥ 0 | `1` | 存在开放 `needs_human` 项时预留的人类优先槽位数；实际预留 = `max(该值, 开放项数)`，上限为槽位总数 | `python/ultimate_coders/attention/capacity.py`（`HUMAN_RESERVE_ENV` / `DEFAULT_HUMAN_RESERVE`） | 设得过大（例如等于槽位总数）：一旦有人工决策在等，**普通工作全部排不上**；设 `0`：决策做出后要排到长任务后面才执行，`needs_human` 失去优先含义。注意没有开放 `needs_human` 时预留恒为 0，所以大值只在有人工工作时才有害 |
| `UC_WATCHDOG_MAX_REPLANS` | 整数 ≥ 1 | `2` | 每个 case 允许的重规划次数上限；用尽后升级给人 | `python/ultimate_coders/proactive/watchdog_input.py`；镜像 `WatchdogConfig::max_replans`（`crates/uc-types/src/governance/proactive.rs:517-563`） | 太小：正常可恢复的停滞过早升级，人力被叫醒；太大：重规划循环（仍受 `max_attempts` 与效果台账约束，不会无限） |
| `UC_WATCHDOG_MAX_ATTEMPTS` | 整数 ≥ 1 | `5` | 每个 case 的尝试上限；达到即 `stop` | 同上 | 太小：正常重试被 `stop` 打断；太大：**用真钱烧同一个失败**，且停滞感知变慢 |
| `UC_PROGRESS_STALL_LIMIT` | 整数 ≥ 1 | `2` | 连续多少次「无证据增量」判定为停滞（同一失败签名 + 文件集不变） | 检测侧 `python/ultimate_coders/agent/progress.py`（`UC_PROGRESS_STALL_LIMIT`）；两侧都用 `2` | 设 `1`：一次无增量就判停滞，抖动明显（正常的两步修复会被打断）；设太大：停滞发现迟钝，浪费尝试预算 |
| `UC_OPERATOR_ID` | 任意字符串 | `operator` | `UC_POLICY_BOOTSTRAP=dev` 时装进快照的人类操作者 principal id（Dashboard 用 `user/dashboard`，二者不同） | `crates/uc-grpc-server/src/governance_boot.rs::bootstrap_policy` | 与 Dashboard 实际发送的身份不一致时，`dev` 快照里的人形操作被拒（`missing_permission`）——只会影响 `dev` 引导，不影响持久化策略 |
| `UC_POLICY_GRANTS_JSON` | JSON 数组 | 未设置 | 显式授权表（持久化策略之外的第二条配置路径）：`[{"kind","id","permissions":[...],"project_id"?}]`；未知权限 token / 不可授权的 kind 一律报错，不静默丢弃 | `crates/uc-grpc-server/src/governance_boot.rs::parse_grants` | 拼错权限名：该条授权不生效并打 error（不会降级成「全允许」）；把 `worker_scale` 授给 `agent`/`worker`：被结构性天花板拒绝 |
| `UC_WORKER_SLOTS_TOTAL` / `UC_WORKER_SLOTS_BUSY` | 整数 ≥ 0 | `0` | **仅后备**：容量投影优先取网关实时 `WorkerRegistry`（`capacity_totals()`：只算心跳新鲜且声明了 contract version 的 worker，`busy = Σ min(load, capacity)`）；只有在注册表里一个可用槽都没有时才用这两个值 | `crates/uc-grpc/src/worker_service.rs`（`capacity_totals`）、`crates/uc-grpc/src/governance/attention_service.rs`（`get_capacity_with`） | 有 worker 时这两个变量被忽略——手工数字不允许掩盖真实池，否则「省下来的槽位」指标就是虚构的 |

> 强制约定：`UC_WATCHDOG_*` 与 `UC_PROGRESS_STALL_LIMIT` 的 Rust 默认值
> （`WatchdogConfig`）与 Python 默认值必须一致，否则「有界」在两侧含义不同。改一处必须同时改另一处，
> 并在 `tests/python/test_proactive_watchdog.py` 与 `cargo test -p uc-types governance::proactive`
> 里都留下断言。

### 1.2 相关但非本层变量（会改变治理面行为）

| 变量 | 影响 | 出处 |
| --- | --- | --- |
| `UC_DASHBOARD_TOKEN` | 设置后 `POST /api/triggers/ingest` 与 `attention.resolve` 要求 Bearer；未设置则按既有约定不校验（由外层部署守门） | `python/ultimate_coders/dashboard/trigger_api.py`、`python/ultimate_coders/dashboard/attention_api.py` |
| `UC_NATS_URL` / NATS 可达性 | 治理桥传输；不可达时 Dashboard 治理端点全部 503 `governance_unavailable`（**不是**空数据） | `governance_client.py` |
| `UC_COMPLETION_CONDITIONS_ENFORCE` | 打开后完成条件才强制校验；默认关闭。与 PR02 的 `verifying` 状态配合使用 | `python/ultimate_coders/agent/completion_conditions.py` |
| `UC_TASK_BACKEND` / `UC_EVENT_BACKEND` / `UC_GRAPH_SHADOW` | 执行面后端，不属治理层；但 `Governance` 与它们共用 `UC_DATABASE_URL` 时需保证同一个库 | `crates/uc-grpc-server/src/main.rs`、`docs/architecture.md` |

## 2. 治理桥：唯一传输契约

Rust 网关是治理状态的**唯一权威**；Python 只是投影。传输是 NATS request/reply：

```
request  → uc.governance.request        {"op": "case.create", "payload": {...}}
response ← reply subject                {"ok": true,  "result": {...}}
                                        {"ok": false, "error": {"code": "...", "message": "..."}}
```

（`python/ultimate_coders/governance_client.py` 模块文档；默认超时 5s。`ok=false` 不等于传输失败：
前者是**网关拒绝**（4xx + code），后者是不可达/超时（503）。这两种必须能区分，否则「网关挂了」和
「策略拒绝了」会被混成一个错误。）

已定义的 op（与 `GovernanceClient` 的方法一一对应）：

| 组 | op |
| --- | --- |
| 提交（PR01） | `submission.submit`、`submission.get` |
| case（PR02） | `case.create`、`case.get`、`case.list`、`case.transition`、`case.run.open`、`case.run.finish` |
| 授权（PR03） | `mandate.issue`、`mandate.get`、`mandate.revoke`、`authorize` |
| 触发（PR04） | `trigger.ingest`、`trigger.subscription.list`、`trigger.subscription.upsert`、`trigger.subscription.delete` |
| 注意力（PR05） | `attention.list`、`attention.raise`、`attention.resolve`、`capacity.get` |
| 主动性（PR06） | `proactive.state.get`、`proactive.mode.set`、`proactive.proposal.submit`、`proactive.proposal.list`、`proactive.proposal.decide`、`proactive.audit.list`、`watchdog.stall.report` |

**权威顺序**：proto（`engine.proto`）定义 gRPC 契约 → NATS 桥**镜像**同一批 op 与同一份 JSON 形状。
两者语义不得分叉；若发现不一致，以 proto 为准并修桥。

这条「不得分叉」在实现上靠两点保证（`crates/uc-grpc/src/governance/mod.rs:10-29`）：每个操作只有
**一个**自由函数（`case_service::create_case`、`trigger_service::ingest` …），tonic 方法与该桥 handler
都调用它；失败映射共用同一套 code——gRPC 侧是 `permission_denied`/`failed_precondition` 等状态码，
桥侧是 `{"ok": false, "error": {"code", "message"}}` 里的同一个字符串。因此 Dashboard 无法从回答内容
判断是哪个传输答的（这正是目的）。

一处**刻意的编码差异**要记住（`crates/uc-grpc/src/governance/bridge.rs:12-17`）：时间在 proto 里是
`int64` Unix 秒，在 JSON 回复里是 RFC 3339 字符串（因为过桥的都是 serde 序列化的领域类型）。Python
Dashboard 两种都接受（`attention/model.py` 的 `_parse_timestamp`）；自己写客户端时不要假设只有一种。
另外，outbox 的投递端只有一条路径：`bridge.rs` 的 `drain_outbox`（feature `messaging`），单趟上限
`OUTBOX_DRAIN_LIMIT = 64`，投递失败即 `schedule_retry`。

## 3. 启动

### 3.1 本地（内存后端，最省事）

```bash
# 1) 治理面单测（不需要任何外部依赖）
cargo test -p uc-types governance
cargo test -p uc-engine governance

# 2) 网关（既有命令；治理 gRPC 接线落地后即带治理面）
cargo run -p uc-grpc-server

# 3) 本地放行策略（仅本地！）
#    UC_POLICY_BOOTSTRAP=dev
#    UC_PROACTIVE_MODE=off        # 默认就是 off，显式写出来便于 review
```

内存后端的语义必须记住：**重启即丢治理状态**（提交记录、case、mandate、审计）。这不是缺陷，而是
`is_durable() == false` 的诚实报告；本地开发/集成测试用它，生产不许用。

### 3.2 生产（PostgreSQL 后端）

```bash
export UC_GOVERNANCE_BACKEND=postgres
export UC_DATABASE_URL='postgresql://…/ultimate_coders'
export UC_POLICY_BOOTSTRAP=none          # 生产不许 dev
export UC_PROACTIVE_MODE=off             # 先观察，再谈 autonomy
export UC_ATTENTION_HUMAN_RESERVE=1
export UC_WATCHDOG_MAX_REPLANS=2
export UC_WATCHDOG_MAX_ATTEMPTS=5
export UC_PROGRESS_STALL_LIMIT=2
export UC_GITHUB_WEBHOOK_SECRET='<真实密钥>'
cargo run -p uc-grpc-server
```

建表是幂等的：`GOVERNANCE_RECORDS_DDL` 用 `CREATE TABLE IF NOT EXISTS governance_records`
（`crates/uc-engine/src/governance/record_store.rs:236-248`），可与既有迁移共存，首写时惰性执行。

> 读取点：`crates/uc-grpc-server/src/governance_boot.rs::choose_record_store`。`memory`（默认）用
> `Governance::in_memory()`；`postgres` 走 `Governance::new(Arc::new(PostgresRecordStore::connect(url, 4).await?))`，
> 连接失败时**降级为内存并打 warning**（而不是拒绝启动），`Governance::is_durable()` 会持续报告 `false`。

### 3.3 Dashboard 侧

`python/ultimate_coders/dashboard/app.py` 已经做了三件事：构造 `GovernanceClient(self._nats_client)`、挂到
`app.state.governance_client`、并把 `governance_routes.governance_routers()` 里的三个 router 都
`include_router` 进去；NATS 连接建立后还会 `attach` 一次（重连场景）。

可观测的预期：

- 桥通：`GET /api/capacity`、`GET /api/attention`、`GET /api/proactive/state`、`GET /api/cases` 正常返回；
- 桥不通：以上端点全部 `503 {"error": "governance_unavailable"}`。**看到 503 说明部署缺桥，不是数据为空**。

## 4. 逐 PR 手工验收（速查）

| PR | 一行判据 | 命令 / 操作 |
| --- | --- | --- |
| PR01 不重复 | 同一 `submission_id` 连发两次只产生一个 task | `cargo test -p uc-engine governance::submission`；协议层看 `SubmitTaskResponse.dedup_replayed`（`engine.proto:288-300`） |
| PR02 可持续 | `active → completed` 被拒，`active → verifying → completed` 通过 | `cargo test -p uc-engine governance::case_store`；`TransitionCase` 跳级必须失败 |
| PR03 不越权 | `external_trigger` 请求 `worker_scale` 被拒；未知工具被拒；带 docker socket 的沙箱档被拒；`ScaleWorkers` 在只有权限、没有操作者层级或一次性审批时被拒 | `cargo test -p uc-engine governance::policy`；`Authorize` 看 `deny_code` |
| PR04 可收敛 | 重放同一 delivery 第二次报 `duplicate` 且不扇出；缺 secret 时 webhook 端点 503 | `cargo test -p uc-engine governance::trigger_inbox`；`python -m pytest tests/python/test_github_trigger_webhook.py tests/python/test_github_reconciler.py tests/python/test_dashboard_trigger_api.py` |
| PR05 少打扰 | `waiting_event` 不改变 `worker_slots_available`，`slots_saved_by_quiet_waiting` +1 | `cargo test -p uc-engine governance::attention`；`GET /api/capacity` 前后对比 |
| PR06 可验证 | 默认 `off`；空证据提案被拒且进审计；停滞到预算上限改升人工 | `cargo test -p uc-engine governance::proactive`、`governance::watchdog`；`python -m pytest tests/python/test_proactive_*.py` |

完整版（含每个测试名）见各 PR 文档的「验收：怎么手工验」一节。Python 测试一律用
`python -m pytest tests/python/<file>`；桥与挂载的通用验收是
`python -m pytest tests/python/test_governance_client.py tests/python/test_dashboard_governance_wiring.py`
（前者钉住「拒绝 vs 不可达」的分类，后者钉住三个治理 router 的挂载与 503 降级）。

## 5. 升级顺序

**必须先升级并重启网关，再升级 worker。** 这条规则不是治理层新加的，而是既有的 contract version
lockstep 规则（`CLAUDE.md` / `AGENTS.md`：`CONTRACT_VERSION` 在
`crates/uc-types/src/envelope.rs` 与 `python/ultimate_coders/nats_worker.py` 必须同批 bump；版本不匹配
时 worker 注册与心跳被**拒绝**，legacy worker 只被观测、永不被派发）。

治理层在此之上还有三条自己的顺序约束：

1. **网关 → Dashboard**：网关是治理状态唯一权威。新 Dashboard 对旧网关的表现是
   `503 governance_unavailable`（安全：不会显示假数据）；旧 Dashboard 对新网关只是不显示治理面，
   执行面不受影响。所以网关先行，Dashboard 后行。
2. **策略先装、再放量**：`UC_POLICY_BOOTSTRAP=none` 的空快照会拒绝一切；上线时应先把策略通过
   `PolicyEngine::install` 装成期望状态并确认 `policy/current` 已有内容，再开放调用方。先把人放进来、
   再补策略，会得到一串难以解释的拒绝。
3. **自治先关（`off` → `observe` → `propose` → `execute`）**：每次只推进一档，并在 `observe` 阶段用
   `GET /api/proactive/audit` 评估误报率。逆序（先开 `execute`）无法从审计里分辨「本来该批」和
   「规则误报」。

worker 侧的变化是**可选上报**（停滞信号 `watchdog.stall.report`）：缺失时退化为没有 watchdog，
尝试预算仍由执行面约束，因此可以晚于网关升级，但**不能早于**网关（否则上报会被旧网关拒）。

回滚：治理层新增的是**旁路状态**，回滚网关不会破坏执行面数据；但 `governance_records` 表会留下
新版本写下的记录，回滚后旧版本不读它们——不要删表，保留以便再次前进时复用（决策与审计是历史，
不是缓存）。

## 6. 故障排查速查

| 现象 | 最可能原因 | 查什么 |
| --- | --- | --- |
| 所有治理端点 503 | NATS 桥没起来 / `uc.governance.request` 没人订阅 | NATS 上是否有订阅；`governance_client` 是否 `is_connected` |
| 重启后提交/case 消失 | 后端是 `memory` | `Governance::is_durable()` 应为 `true`；`UC_GOVERNANCE_BACKEND`/`UC_DATABASE_URL` |
| 所有治理操作被拒 | 空策略快照（`UC_POLICY_BOOTSTRAP=none` 且未 install） | `policy/current` 是否有内容；`deny_code` 是否为 `missing_permission` |
| 提案全被拒且 detail 含 `propose_without_evidence` | 求值器没给证据 | 提案 `evidence` 是否为空（这是设计行为，不是 bug） |
| 停滞任务永不升级给人 | `UC_WATCHDOG_MAX_REPLANS` 设得过大 | 与 `max_attempts` 对比；看 `watchdog_decision` 命名空间的历史 |
| 有人工决策但决策做完仍排队 | `UC_ATTENTION_HUMAN_RESERVE=0`，或调度器尚未消费容量快照 | 容量快照与 [pr05-attention.md](pr05-attention.md) §9 的未实现项 |
| webhook 全部 503 | `UC_GITHUB_WEBHOOK_SECRET` 未设置 | 端点文档里的 fail-closed 行为（`trigger_api.py`） |
