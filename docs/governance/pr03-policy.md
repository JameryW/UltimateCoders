# PR03 — Policy：谁能做什么，以及工具与沙箱的硬边界

代码：`crates/uc-types/src/governance/policy.rs`（principal / permission / grant / 工具白名单 /
沙箱档位，纯函数）、`crates/uc-engine/src/governance/policy.rs`（`PolicyEngine`：快照持久化、工具级检查、
mandate 生命周期）。proto：`crates/uc-grpc/proto/engine.proto:1170-1198`（`Authorize`）。

PR03 编码的就是「不越权」：**触发器、PR 评论、记忆条目、模型输出，都不能扩大工作所依据的授权**。
两层机制，缺一不可：

- **结构性**（编译期/构造期硬编码）：`PrincipalKind::may_hold` 是不可配置的天花板，
  违规 grant 在构造快照时被拒；
- **评估性**（每次检查都 fail-closed）：未知工具拒绝、缺失/过期/吊销的 grant 拒绝、陈旧策略版本拒绝。

## 1. Principal：谁在问

六种调用方（`policy.rs:26-42`）：

| kind | 含义 | 典型来源 |
| --- | --- | --- |
| `user` | 人类操作者 | Dashboard / CLI / 带用户 token 的 API |
| `service` | 可信内部服务 | gateway 与 gateway、带 token 的 CI runner |
| `agent` | 自治 agent（planner / reviewer / executor 循环） | Orchestrator 内部 |
| `worker` | 执行被派发子任务的分布式 worker | Python Worker |
| `external_trigger` | 入站集成事件 | GitHub webhook、Lark 消息、cron tick |
| `anonymous` | 无或无法验证的凭据 | 未认证请求 |

`Principal { kind, id }`；`is_authenticated()` 只有 `anonymous` 为假
（`policy.rs:85-127`）——也就是说 `external_trigger` **算已认证**（webhook 由服务端验签），
但它仍然拿不到授权类权限。

## 2. 结构性天花板（kind × permission）

15 个权限（`policy.rs:136-167`），其中 5 个是**授权类**：`mandate_issue`、`mandate_revoke`、
`worker_scale`、`merge_grant`、`proactive_execute`（`policy.rs:190-202`）。
`may_hold` 矩阵（`policy.rs:56-82`，`Y` = 可持有）：

| permission | 授权类 | user | service | agent | worker | external_trigger | anonymous |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `task_submit` | | Y | Y | Y | Y | Y | Y |
| `task_read` | | Y | Y | Y | Y | Y | Y |
| `task_control` | | Y | Y | Y | Y | Y | Y |
| `case_create` | | Y | Y | Y | Y | Y | Y |
| `case_read` | | Y | Y | Y | Y | Y | Y |
| `case_control` | | Y | Y | Y | Y | Y | Y |
| `trigger_ingest` | | Y | Y | Y | Y | Y | Y |
| `memory_write` | | Y | Y | Y | Y | Y | Y |
| `attention_resolve` | | Y | Y | Y | Y | Y | Y |
| `proactive_propose` | | Y | Y | Y | Y | Y | Y |
| `mandate_issue` | **是** | Y | Y | N | N | N | N |
| `mandate_revoke` | **是** | Y | Y | N | N | N | N |
| `worker_scale` | **是** | Y | Y | N | N | N | N |
| `merge_grant` | **是** | Y | Y | Y | Y | N | N |
| `proactive_execute` | **是** | Y | Y | Y | Y | N | N |

三处值得强调：

- `external_trigger` / `anonymous` 被写成「`!permission.is_authority_granting()`」，不是一张逐项白名单
  ——新增授权类权限时它们**自动**被排除，不需要记得改这里；
- `merge_grant` 是授权类，但 agent/worker **可以**持有：合并授权是「写 `main`」的许可，由人类
  principal 签发后交给执行方，属于正常下行授权；
- `trigger_ingest` 属于 `service`（验签并转发 webhook 的那个服务），**不属于事件本身**。事件没有
  调用方身份，这条区分是「触发器不带授权」的落点（见 [pr04-triggers.md](pr04-triggers.md)）。

gRPC 层在结构性天花板之外再加一道**身份声明**闸口：`x-uc-principal-kind` / `x-uc-principal-id`
元数据允许调用方声明身份，但 `external_trigger` 与 `anonymous` **一律拒绝**（
`crates/uc-grpc/src/governance/policy_interceptor.rs:19-26`，`resolve_principal`）；未声明身份的请求
解析为 `service/dashboard`——操作者界面（同文件 `:27-31`）。含义是：入站 webhook 只能以「验签它的
那个 service」出现，永远不能自称事件本身；而「声明 anonymous」也不会变成提权（它是被拒绝，不是被
重新解释）。

违反上限的 grant 在**构造期**就被拒（`PolicySnapshot::with_grant` → `GrantError::ForbiddenPrincipalKind`，
`policy.rs:505-517`）。这是有意的：配置错误应当在装载策略时炸，而不是等某个调用方在凌晨两点撞上。

## 3. Grant 与评估顺序

`Grant`（`policy.rs:295-344`）：`principal`、`permissions`、可选 `project_id` 范围、可选
`expires_at`、`revoked` 标志、`source` 字符串（"bootstrap"/"config"/"operator"，仅用于溯源）。
`permits()` 先看吊销与是否包含该权限，再看项目范围：无项目范围 = 全局；有范围但请求没带项目 = 拒。

`PolicySnapshot::evaluate`（`policy.rs:527-574`）的顺序是固定的：

```
1. 未认证                          → unauthenticated
2. kind 天花板不允许               → forbidden_principal_kind
3. 逐条 grant：吊销的跳过（记 saw_revoked）→ 过期的跳过（记 saw_expired）→ 范围匹配则放行
4. 无匹配：有吊销记录 → grant_revoked；有过期记录 → grant_expired；否则 → missing_permission
```

第 4 步把「没给」和「曾给过但被吊销/过期」分开报，正是为了让审计能区分
`no credential` 与 `credential revoked`（`DenyReason` 文档，`policy.rs:346-387`）。
测试：`unauthenticated_is_denied`、`missing_permission_is_reported_as_missing_not_as_auth`、
`revoked_and_expired_grants_are_distinguished`、`project_scoped_grant_only_covers_its_project`。

## 4. ActionClass：把「副作用等级」映射到权限

`ActionClass`（`policy.rs:226-271`）是 mandate 表达范围的粒度，也是权限映射表：

| ActionClass | 需要的 permission |
| --- | --- |
| `read` | `task_read` |
| `write` / `execute` | `task_submit` |
| `dispatch` | `task_control` |
| `push` / `merge` | `merge_grant` |
| `external_notify` | `memory_write` |
| `scale` / `admin` | `worker_scale` |

于是 mandate 可以用「副作用等级」说话，而不是逐个工具名：`action: [read, write]` 就是
「可以读和改，但永不 push、永不 merge」。

## 5. 工具白名单：fail-closed

`ToolPolicyTable::builtin()`（`policy.rs:652-681`）是当前 UC 沙箱暴露工具的全部清单：

| 工具 | ActionClass | 需要 mandate scope |
| --- | --- | --- |
| `Read`、`Glob`、`Grep`、`LS`、`NotebookRead`、`TodoRead` | `read` | 否（`ToolPolicy::read_only`） |
| `Edit`、`Write`、`NotebookEdit`、`MultiEdit`、`memory_write` | `write` | 是 |
| `Bash`、`BashOutput`、`KillBash`、`docker_run` | `execute` | 是 |
| `Task`、`Agent` | `dispatch` | 是 |
| `git_push` | `push` | 是 |
| `merge` | `merge` | 是 |
| `github_comment`、`github_issue_update` | `external_notify` | 是 |
| `scale_workers` | `scale` | 是（并标记 `human_approval`） |

表里没有的工具**一律拒绝**，且不会「默认当成只读」：`requirement()` 对未知工具的返回值是
`known = false`、`action = admin`、`requires_mandate_scope = true`、`human_approval = true`
（`policy.rs:687-697`），调用方必须先看 `known`（`policy.rs:699-722`）。
这就是「新增工具必须显式登记」的强制点。
测试：`unknown_tool_is_denied_and_known_tools_have_explicit_classes`、
`unknown_tool_permission_check_fails_closed`。

`PolicyEngine::authorize_tool` 把四步串起来，且**拒绝时只报第一个失败项**，让审计可读
（`crates/uc-engine/src/governance/policy.rs:218-267`）：

```
① known?           否 → tool_not_permitted
② permission 检查  否 → 具体 deny reason（unauthenticated / forbidden_principal_kind / …）
③ 只读工具         → 放行
④ 需要 case 与 mandate：无 case → mandate_missing；无 mandate → mandate_missing；
   mandate 不覆盖 (tool, action) → mandate_out_of_scope / mandate_revoked / …
```

测试：`tool_check_requires_a_mandate_scope_only_for_state_changing_tools`。

**`human_approval` 是强制的（不是标记）**：`PolicyEngine::authorize_tool` 在权限检查之后、mandate 范围检查
之前，对标记了 `human_approval` 的工具（当前是 `scale_workers` 与 `docker_run`）额外要求操作者权限
`proactive_execute`，否则拒绝并给出 `deny_code=tool_requires_human_approval`
（`crates/uc-engine/src/governance/policy.rs` 的 `authorize_tool`，测试
`human_tier_tools_require_the_operator_permission`、`human_tier_gate_passes_before_the_mandate_scope_check`）。
因为 `PrincipalKind::may_hold` 拒绝 agent/worker 持有 `proactive_execute`，这一条同时把「agent 自己给自己
批准高风险工具」堵死。

### 5.1 人工审批回路（`approval.rs` / `approvals.rs` / `approval_service.rs`）

「操作者层级」不是唯一出路。human-tier 工具还有第二条门，是一条**有记录、单人判、一次性**的回路：

```
agent  approval.request(tool, case, action_digest, reason)   ->  Pending（默认 15 分钟，上限 1 天）
human  approval.decide(id, approve=true, note)               ->  Approved（并 resolve 对应 needs_human 项）
call   authorize_tool(...)                                    ->  消费许可 -> Used（下一次再要一份）
```

| 规则 | 为什么 |
| --- | --- |
| 许可 id 由 `(tool, case, requester, action_digest)` 派生 | 重试收敛到同一条待办，而不是把人的工作清单刷满 |
| 只有 `user` 能决定，且不能决定自己发起的请求 | 「服务说同意」「agent 自己批自己」都不是人的同意 |
| 决定还要求 `proactive_execute`（两道门） | 操作者 token ≠ 被等的那个人的同意 |
| **先偷看、后消费**：`find_approved` 读，`settle_tool_decision` 在所有其它检查通过后才 `consume` | 一次因为别的原因（缺 mandate、项目越界）被拒的调用，不该烧掉人的许可 |
| 一次性（`Used`）+ 过期（`Expired`） | 一次同意不是一张空白支票；过期由 sweeper（`expire_stale`）清理 |
| 请求会 raise 一条 `needs_human` 注意力项 | 人照常在已有的待办界面看到它；该项只是**指针**，丢了不影响许可本身 |

一次被消费的许可能在任何后续调用里被审计（`ApprovalState::Used` + `used_at` + `decided_by`），
而拒绝码仍然是 `tool_requires_human_approval` —— 调用方只需重试「申请」，不用理解内部状态。

测试：类型层 `only_a_human_who_is_not_the_requester_can_decide`、`a_permit_is_single_use`、
`coverage_is_tool_and_case_exact`；存储层 `peeking_does_not_spend_the_permit`、
`the_sweeper_expires_what_nobody_answered`；策略层 `a_human_approval_unlocks_a_human_tier_tool_for_exactly_one_call`、
`a_permit_is_not_spent_when_a_later_check_denies`；服务层 `only_a_human_with_operator_authority_can_decide`。

仍留空的：仪表盘只有 HTTP 面（`/api/approvals`），没有专门的 React 审批面板——它出现在注意力待办里，
点开仍是「注意力」视图。

## 6. 沙箱硬化档

`SandboxSecurityProfile`（`policy.rs:729-874`）刻意只保留**安全相关**子集，以便网关在容器启动**之前**
就拒绝不安全配置（而不是事后发现）。`validate()` 的判定项：

| 违规 | 触发条件 | 为什么 |
| --- | --- | --- |
| `privileged_container` | `privileged = true` | 等于给了宿主机 root |
| `docker_socket_mounted` | `docker_socket_mounted = true` | **无条件拒绝**：docker socket 等价宿主机 root；`ScaleWorkers` 在宿主机侧跑，永不在 worker 沙箱内 |
| `host_network` | `network_mode` 为 `host` | 绕开网络隔离 |
| `host_pid` | `host_pid = true` | 看见并干扰宿主进程 |
| `writable_host_mount` | `writable_host_mounts` 非空（逐条报路径） | 可写宿主挂载 = 逃逸面 |
| `runs_as_root` | `run_as_root = true` | 容器内提权 |
| `seccomp_unconfined` | `seccomp_profile` 为空或 `unconfined` | 失去系统调用过滤 |
| `no_resource_limits` | 内存与 CPU 上限**都为 0** | 一个沙箱可吃满宿主 |

`hardened_worker()`（`policy.rs:857-873`）给出可用的安全默认值：`bridge` 网络、
非 root、`seccomp: runtime/default`、无 socket、无宿主可写挂载、2 GiB 内存、2000m CPU。
测试：`hardened_worker_profile_has_no_violations`、`docker_socket_and_privileged_are_always_refused`、
`missing_seccomp_and_limits_are_violations`；
引擎侧 `sandbox_validation_blocks_docker_socket`。

> **未实现**：`read_only_rootfs` 字段存在但**不参与**判定；`hardened_worker()` 自身就是
> `read_only_rootfs: false`，所以把它变成硬性要求会先打翻现有 worker 部署。审计时不要把它当强制项。

## 7. ScaleWorkers：最高爆炸半径的单点

扩缩容会 `docker compose` 外呼，是网关暴露的最高爆炸半径操作，因此有独立入口
`PolicyEngine::authorize_scale_workers`（`crates/uc-engine/src/governance/policy.rs:183-197`），
同时强制权限（`worker_scale`）与结构性天花板（agent/worker/trigger/anonymous 全部禁止）。
测试：`agent_cannot_scale_workers`、`external_trigger_cannot_hold_authority_granting_permission`、
引擎侧 `scale_workers_requires_its_own_permission`。

**已接线**：`WorkerService::scale_workers` 在处理任何动作（含 `deregister`）之前先解析 principal 并调用
`require_scale_workers`（`crates/uc-grpc/src/worker_service.rs:745-752`）；principal 从 metadata
`x-uc-principal-kind` / `x-uc-principal-id` 读取，缺失按 `service/dashboard` 处理。空策略快照下所有调用
都被拒（`missing_permission`），即**默认拒绝扩缩容**。集成测试见
`crates/uc-grpc/tests/governance_services.rs`（ScaleWorkers denial）与
`cargo test -p uc-grpc governance` 的 72 个单测。

> 部署提醒：`UC_POLICY_BOOTSTRAP=dev` 的 dev 快照**不含** `worker_scale` 给 `service:dashboard`/`user:dashboard`
> （只有 `user:operator` 持全部权限）。所以本地要让 Dashboard 的扩缩容按钮可用，要么以 operator 身份调用，
> 要么在 `UC_POLICY_GRANTS_JSON` 里显式授予——这是「不越权」的代价，不是 bug。

## 8. 吊销与策略版本陈旧

两条「时间维度」的防线：

1. **吊销立刻生效**：`Mandate::permits` 首查 `revoked`，先于任何 scope 匹配
   （`mandate.rs:418-436`），所以「缓存了旧 mandate、范围恰好匹配」不能绕过吊销。
   `PolicyEngine::revoke_mandate` 还会把 id 记进快照的 `revoked_mandates`，让在途检查快速失败
   （`crates/uc-engine/src/governance/policy.rs:366-370`）。
2. **收紧后的策略版本**：`PolicySnapshot.minimum_accepted_version` 存在时，
   `check_version(provided)` 对低于下限的请求直接拒 `stale_policy_version`
   （`policy.rs:576-589`）。这样「策略已收紧，但在途请求还带着旧版本号」会重新走评估，而不是被
   默认放行。测试：`stale_policy_version_is_rejected_after_tightening`。

`PolicyEngine::install` 每次装载都会推进版本号（除非导入的快照版本更新），并持久化到
`policy/current`，因此重启不会把策略「回退一代」
（`crates/uc-engine/src/governance/policy.rs:143-168`；测试 `snapshot_round_trips_through_the_store_and_bumps_version`）。

`UC_POLICY_BOOTSTRAP=dev` 会装载 `development_snapshot()`：一个 user 持有全部 15 个权限、
外加一个 `dashboard` service 只持有读+resolve（`crates/uc-engine/src/governance/policy.rs:392-436`）。
空快照则**拒绝一切**（测试 `empty_snapshot_denies_everything`）。默认值与此变量的危险用法见
[operations.md](operations.md)。

`DenyReason` 共 16 种（`policy.rs:348-410`），每种都能落到一个短语：
`unauthenticated` / `missing_permission` / `grant_revoked` / `grant_expired` /
`forbidden_principal_kind` / `stale_policy_version` / `mandate_missing` / `mandate_revoked` /
`mandate_expired` / `mandate_out_of_scope` / `mandate_widening_denied` / `tool_not_permitted` /
`tool_requires_mandate_scope` / `sandbox_violation` / `budget_exhausted` /
`proactive_mode_disallows`。这套代码是审计与告警的稳定键——不要改字符串。

## 9. 验收：怎么手工验

1. `cargo test -p uc-types governance::policy`
   （天花板、grant 状态区分、未知工具、沙箱、版本陈旧、权限 token 往返）。
   另有 `permission_wire_tokens_round_trip` 保证 wire 字符串稳定。
2. `cargo test -p uc-types governance::mandate`（范围匹配、时效、委派收窄）。
3. `cargo test -p uc-engine governance::policy`
   （`empty_snapshot_denies_everything`、`operator_can_issue_and_revoke_a_mandate`、
   `second_live_mandate_for_a_case_is_refused`、`delegation_cannot_widen`、
   `scale_workers_requires_its_own_permission`、`sandbox_validation_blocks_docker_socket`）。
4. gRPC 可用后，用 `Authorize` 逐个打验证（`engine.proto:1170-1198`）：
   - `principal_kind=external_trigger` + `permission=worker_scale` → `allowed=false`，
     `deny_code=forbidden_principal_kind`；
   - `tool=some_new_tool` → `deny_code=tool_not_permitted`；
   - `sandbox_profile_json` 里带 `docker_socket_mounted: true` → `deny_code=sandbox_violation`；
   - `case_id` 指向的 case 无 mandate 的写类工具 → `deny_code=mandate_missing`。

## 10. 未实现 / 有意留空

| 项 | 现状 | 原因 |
| --- | --- | --- |
| 审批的 React 面板 | 只有 HTTP 面（`/api/approvals`）与注意力待办；没有专门的审批 UI | 审批先要「可审计 + 一次性」正确，界面后置；现有待办视图已经能看见它 |
| `ScaleWorkers` 的授权接线 | **已落地**（见 §7）：`worker_service.rs:745-752` 前置鉴权，空快照默认拒绝 | — |
| 工具**参数**级检查 | 未实现（只检查工具名与 action 类） | 参数语义因工具而异（例如 `Bash` 的命令串），假装能通用检查会给出虚假安全感 |
| `read_only_rootfs` 强制 | 未实现（见 §6） | 现有 worker 镜像不以只读根文件系统运行 |
| Graph-backed fencing 相关权限 | 未纳入治理层 | 执行面已有独立设计（`docs/agent-os-architecture.md` 的恢复不变量），重复会造第二权威 |
| 策略编辑器 / 运行时改策略的 UI | 只提供 `install` 原语与 `policy/current` 文档 | 改策略是高风险操作，先保证「改动可审计 + 版本可追溯」，UI 后置 |

## 11. 生产路径评估（方案 PR03 明确要求评估的四项）

本节回答「这些边界在生产路径上到底生效了没有」，只写能指向代码的事实。

### 11.1 当前 Policy 的重验证

| 事实 | 落点 | 结论 |
| --- | --- | --- |
| 每次调用都重新求值，不缓存「已放行」结果 | `PolicySnapshot::evaluate`（`crates/uc-types/src/governance/policy.rs`）是纯函数；`PolicyEngine::authorize*` 每次读当前快照 | 生效 |
| 陈旧策略版本被拒 | `PolicySnapshot::check_version` + `DenyReason::StalePolicyVersion`；`AuthorizeRequest.policy_version` 可携带 | 生效（调用方携带版本时） |
| 吊销立即生效 | `PolicyEngine::revoke_mandate` 同时写 mandate 记录与快照 `revoked_mandates` 负缓存；`Mandate::permits` 先查吊销再查范围 | 生效 |
| 重启后策略不回落 | `bootstrap_policy`（`crates/uc-grpc-server/src/governance_boot.rs`）先 `load_persisted`，命中即跳过环境引导并打日志 | 生效（`UC_GOVERNANCE_BACKEND=postgres` 时持久；`memory` 时重启回到环境引导，日志会说明） |

### 11.2 Graph-backed Fencing

**未纳入治理层，且这是有意的。** Fencing token 与 attempt 代次属于执行面（`crates/uc-engine/src/graph_store.rs`
的 attempt/epoch 语义、`docs/architecture.md` 的 Durable Runtime 一节）。治理层只回答「谁、凭什么」，
不持有 DAG 也不持有 fencing token；在治理层复制一份会制造第二权威，正是方案「核心原则」禁止的。
治理层对齐方式：`WorkCase.runs[].task_id` 只记录 run 与逻辑 task 的链接，不表达节点级所有权。

### 11.3 完成条件（Completion Conditions）

- 评估仍在执行面：`python/ultimate_coders/agent/completion_conditions.py`；治理层只把结果作为不透明 JSON
  存在 `WorkCase.completion_conditions`，不解释它（避免两套判定）。
- 与状态机的对齐是硬约束：`CaseStatus::Verifying → Completed` 是**唯一**进入 `Completed` 的边，
  `Active → Completed` 恒为非法（`crates/uc-types/src/governance/workcase.rs::can_transition_to`，
  测试 `active_case_cannot_jump_to_completed`）。也就是说「成功必须经独立验证」在治理层无法被绕过。
- **生产启用状态**：`UC_COMPLETION_CONDITIONS_ENFORCE` 默认关闭（既有行为），因此当前生产路径上
  `Verifying` 多为短驻状态而不是长驻验证窗口；打开该开关后两者才完全对齐。这一点是配置项，不是治理层缺陷。

### 11.4 沙箱在生产路径的启用情况

| 检查 | 是否在生产路径生效 | 说明 |
| --- | --- | --- |
| Docker socket 挂载 / privileged / host network / host pid / 可写宿主挂载 / root / seccomp | **按需生效**：`AuthorizeRequest.sandbox_profile_json` 携带 profile 时由 `SandboxSecurityProfile::validate` 判定 | 网关不会自己去读 `docker-compose.yml`，因此这是「调用方把配置交上来才检查」的边界；`ScaleWorkers` 在宿主机侧执行，从不进沙箱 |
| 资源上限缺失 | 同上（`no_resource_limits`） | 同上 |
| `read_only_rootfs` | **不强制**（字段存在、不参与判定） | 现有 worker 镜像非只读根；设为强制会先打翻部署，故留为审计项而非硬闸 |
| worker 实际容器启动 | 不经过治理层 | 容器由 compose/`UC_SCALE_HOSTS` 拉起，属既有部署面；治理层只在「谁有权发起」这一层设闸 |

结论：PR03 的四项评估都给了明确落点；两处「未强制」（`read_only_rootfs`、compose 实际配置读取）是
**已知空白**而不是遗漏，列在 §10 与本节表格里，审计时按此判定。