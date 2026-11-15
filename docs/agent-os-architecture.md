# Agent OS 架构：确定性内核 + 概率性智能

本文把「Harness Engineering」的分析落到本仓库的实现边界上：哪些层已经存在、
状态归谁所有、还缺什么、下一步按什么顺序补。它描述的是**升级方向与不变量**，
不是对现有行为的重新叙述——现状部分逐项标注了对应代码与测试，缺口部分不粉饰。

核心判断只有一句：**执行一致性由确定性内核负责，LLM 只做可替换的决策**。
内核可以调用 LLM，但不能假设同一请求在恢复时产生相同输出，因此模型调用
的结果必须落盘，恢复时优先读取已提交的决策而不是重新推理。

## 1. 四层边界

| 层 | 核心问题 | 本仓库的落点 |
|---|---|---|
| Agent Harness | 模型如何正确使用环境 | 沙箱与插件注册表、MCP 工具面、env 白名单、上下文组装 |
| Agent Loop | Agent 如何持续改进结果 | Worker 的 attempt 循环、review、step 条件求值 |
| Workflow Engine | 任务如何可靠执行 | Orchestrator 的 subtask DAG、运行记录、终态 outbox、MergeArbiter |
| Agent OS | 多任务/多 Agent 如何共享基础设施 | Gateway + NATS 调度、Workspace 所有权、沙箱隔离、Inference 资源 |

四层不能相互替代：LangGraph 能表达 Loop、Temporal 能提供持久执行，但都不会
自动给出本仓库已有的工具权限面、沙箱隔离与跨 Worker 资源协调。反过来，
本仓库也不该把「持久化执行」重新实现一遍——`runtime_state.py` 提供的是
**记录与恢复的基座**，不是通用工作流引擎。

## 2. 状态权威：每个事实只有一个所有者

| 事实 | 唯一权威 | 说明 |
|---|---|---|
| 任务生命周期与子任务状态 | Gateway 运行记录（Postgres/SQLite） | `tasks` 表由 Gateway 写；Worker 只上报 |
| 终态事件与投递状态 | `result_outbox` 命名空间 | 带租约、指数退避、交付后压缩 |
| 外部副作用（推送/合并） | MergeArbiter + git 远端 | 冲突以 merge-time 为权威 |
| 工作区所有权 | Workspace 租约 | 沙箱只持有临时执行环境 |
| 决策与上下文 | Harness 的 attempt 内 | 不跨 attempt 复用未提交的推理 |
| 知识层证据与声明 | RuntimeState 的 `knowledge_evidence` / `knowledge_claims` / `attempt_chain` 命名空间（Postgres/SQLite） | 证据、校验、声明各是一条记录；Qdrant / TiKV / 版本化知识页只是**可重建投影**，不反写权威（D1） |
| 长期记忆 | Memory 层 / hy-memory | **不作为**恢复依据；晋升进长期记忆必须过认知门（D4） |
| 推理服务 | 无业务状态 | KV Cache 仅是性能层，可重建 |

这条表是排障的起点：出现不一致时，先问「这两个值谁是权威」，而不是让两套
状态各自演化。典型反例是让 Workflow 与 Agent Loop 各存一份任务状态——
恢复时必然对不上。

## 3. 实体模型

| 实体 | 本仓库对应 | 备注 |
|---|---|---|
| Task | Gateway 的 task 记录 | 用户意图与验收标准 |
| Run | 一次 dispatch 会话 | 重启后由 Gateway 快照恢复 |
| Step | `Subtask` / 其 `steps[]` | 可独立推进的工作单元 |
| Attempt | `dispatch_retry_count` / attempt 循环 | 可递增，允许失败重来 |
| Effect | outbox 记录 + git 推送/合并 | **逻辑操作**，重试应保持同一身份 |

`attempt` 与 `effect` 不能混用：一个 subtask 重试三次，不应产生三条终态事件，
更不应发送三次外部通知。当前 outbox 以 `task:subtask:attempt` 为键——
这正是下一步要收敛的点（见 §5）。

## 4. 已具备的确定性内核能力

- **持久记录 + 版本化读写**：`python/ultimate_coders/runtime_state.py` 的
  `mutate`/`versioned_get` 以命名空间 + 版本做 CAS 式更新，SQLite 与
  PostgreSQL 同构；`test_runtime_state.py` 覆盖冷启动与并发。
- **投递账本**：`python/ultimate_coders/outbox.py` 的 claim/renew/finish
  带租约与指数退避；`test_inference_reliability.py` 里的
  `test_result_outbox_survives_disconnect_and_worker_restart` 钉死重启后的
  不重复投递。
- **重放不等于重推**：`test_lost_submission_response_never_creates_another_job`
  与 `test_restart_adopts_saved_remote_id_without_submission` 证明恢复走
  「读已提交记录」而不是「重新提交」。
- **副作用去重**：`tests/python/test_nats_dedup_integration.py` 验证
  `msg_id` 去重窗口对重发的折叠，以及**新 attempt 不被误折叠**。
- **工作区隔离与合并仲裁**：`python/ultimate_coders/agent/workspace.py`
  的租约 + `tests/python/test_merge_arbiter.py` 的冲突检测/同文件非重叠合并。
- **权限面**：`tests/python/test_sandbox_env_allowlist.py` 与 hy-memory 的
  allowlist 测试证明未知身份拿不到额外凭证，`OPENAI_API_KEY` 不因记忆后端
  而进入共享层。
- **副作用账本**：`python/ultimate_coders/effect_ledger.py` 把「一次逻辑外部
  操作」与「多次执行尝试」分开。状态机 `PREPARED → IN_FLIGHT →
  {CONFIRMED, FAILED, UNKNOWN}`，`UNKNOWN → RECONCILING →
  {CONFIRMED, PREPARED}`。**`UNKNOWN` 不等于 `FAILED`**：传输中断只能先对账，
  只有确定未生效才回到 `PREPARED` 允许重试；`CONFIRMED` 终态下重复调用返回
  记录结果而不是再次执行。合并发布与 Worker 分支推送均已接入
  （`MergeArbiter` 与 `WorkspaceManager.release()`）：以（远端、分支、
  待发布提交）构造稳定 effect id，推送报错时用 `ls-remote` 对账，
  已落地则报 `already_pushed` 而非重试。MetaInfer create POST 为第一个非 git
  kind（`metainfer-create`），默认关闭，由 `UC_EFFECT_LEDGER_METAINFER` 打开；
  终止控制 POST 为第二个（`metainfer-kill`），由 `UC_EFFECT_LEDGER_METAINFER_CONTROL`
  打开，丢响应记 `UNKNOWN` 而不是「终止未确认」的不可判状态。
- **任务契约版本化**：`contract.py` 以「原始请求文本」的归一化摘要为契约身份
  （仅折叠 CRLF/行尾空白/首尾空行，措辞变化——哪怕一个否定词——必须改变摘要），
  按任务记录版本。同一任务的两次派发若契约摘要不同，Worker **拒绝执行**并上报
  `contract_changed`（含 v旧→v新），由编排层重新规划；不携带 `user_request` 的
  旧式派发没有契约可比，直接放行——subtask 描述不是契约，兄弟子任务本就不同
  （`tests/python/test_contract_versioning.py`）。
- **Progress Detector**：`progress.py` 以「证据增量」而非尝试次数判定重试价值——
  归一化错误签名（数字/十六进制/ANSI/空白折叠）、stderr 签名与文件集合三项全部
  未变即视为停滞；连续 N 次（`UC_PROGRESS_STALL_LIMIT`，默认 2）无增量则停止
  循环，把 `loop_detected` 写入终态失败事件并关闭外层 dispatch 重试
  （`tests/python/test_progress_detector.py`）。它只判定「是否还值得再试」，
  不决定「改做什么」——`progress.py` 仍无 replan API；V8 P3 仅在 proactive
  层对 watchdog `replan` 发出结构化提案（`UC_REPLAN_PROPOSAL_ENFORCE`），不执行新 DAG。
- **任务级预算账本**：`budget.py` 在每次执行前做一次准入、执行后按上报记一笔账，
  三种维度独立（token / 成本 / 墙钟时间，`UC_TASK_TOKEN_BUDGET` /
  `UC_TASK_COST_BUDGET_USD` / `UC_TASK_TIME_BUDGET_SECS`）。耗尽时**在沙箱创建前**
  拒绝，终态事件带 `budget_exhausted` 与用量，账本本身保留——提高上限后用更高
  attempt 重新派发即可继续（`tests/python/test_budget_ledger.py`）。两条诚实边界：
  未上报的 usage 计入 `unreported` 而**不当作 0**，因此所有总量都是下界；准入在
  执行前检查，已在飞的 N 个子任务仍会完成，最坏超额为「每在飞子任务一份」。
  未配置任何上限时完全不读写状态账本，默认路径零开销。
- **终态 outbox 的逻辑身份**：`result_outbox` 的键收敛为 `task:subtask`，
  attempt 存于记录内——更高 attempt 取代旧结果、迟到的低 attempt 不覆盖、
  同 attempt 同内容指纹视为重发。消息 id 含（attempt, 内容指纹），因此新结果
  不会被 NATS 去重窗口折叠，而真正的重发仍会被折叠。
- **模型路由**：`routing.py` 把「用哪个模型、给多少推理预算」变成显式的
  (阶段, 风险) 查表。阶段取文档词汇表（planning / tool-selection /
  implementation / verification / recovery）；风险由工作形态推导而非模型
  判断——声明的只读能力（review 模板禁写）高于一切关键词，危险关键词
  （deploy/release/migrate…）或 ≥3 个文件约束为 high，只读关键词为 low，
  其余（含无法识别的形态）为 medium：未知工作不假定便宜，这是唯一无法事后
  挽回的方向。失败分类回答「还值不值得再调模型」：传输/过载 → 原样重试；
  凭证/权限/端点配置 → 交网关，不重试；请求畸形（schema 错）→ 一次降档
  重试（lightweight repair，预算按 `UC_ROUTE_REPAIR_SCALE` 收缩，默认减半，
  且始终从工作的常规阶段单元格推导，不会逐次 compounding）；未分类 →
  重试，但由 Progress Detector 的证据门限界定——这是当日行为，分类器不
  假装知道更多。两条诚实边界：未配置 `UC_ROUTE_TABLE` 时每个决策都是恒等，
  默认路径零行为变化；沙箱 agent 的子进程自有工具循环，worker 侧只把路由
  决策记入 `subtask_started` / 重试事件（适配器尚无按子任务选型的键），
  缺口留在事件流里可见而不是被粉饰（`tests/python/test_model_routing.py`）。

## 5. 差距（按优先级）

| 差距 | 状态 | 说明 |
| --- | --- | --- |
| Decision Record（V8 P0） | **落地** | 派发 attempt 级审计 join：`decision_record` 命名空间 + Worker start/terminal 写入 + `GET /dashboard/api/decisions`；不含 prompt 内容。P9 起 Dashboard 任务详情里有只读 trail（verdict / route / context 指针 / 本地效果），P10 起与 context manifest 同契约分页（`order=recent` + `before`），P11 起可按子任务收窄且分页留在该范围内；排序与计数归端点。见 [v8-upgrade/map.md](architecture/v8-upgrade/map.md)、[D5](architecture/v8-upgrade/issues/D5-decision-record-shape.md)、[D19](architecture/v8-upgrade/issues/D19-decision-record-dashboard-trail.md)、[D20](architecture/v8-upgrade/issues/D20-decisions-trail-paging.md)、[D21](architecture/v8-upgrade/issues/D21-decisions-trail-subtask-scope.md)、[ADR 0001](adr/0001-v8-as-agent-decision-evidence-checklist.md)。 |
| MetaInfer create EffectLedger（V8 P1） | **落地（默认关闭）** | 首个非 git 副作用收据：`kind=metainfer-create`，由 `UC_EFFECT_LEDGER_METAINFER` 打开；`remote_jobs` 仍为操作态。push-on-release 编码路径会惰性绑定 RuntimeState，使 `push-branch` 收据不再静默跳过。`RECONCILING` 与 `IN_FLIGHT` 同样带 soft lease：占用的对账者还活着会拒第二个，租约过期（或旧记录无租约）则原地接管，不会把 effect 永久卡在对账中（T37）。见 [spec-p1](architecture/v8-upgrade/spec-p1.md)、[D7](architecture/v8-upgrade/issues/D7-metainfer-effect-kind.md)。 |
| Coding Champion/Challenger（V8 P2） | **落地（默认关闭）** | 成功后同契约一次 report-role challenger：`UC_CODING_CHALLENGER_ENFORCE`（可选 `UC_CODING_CHALLENGER_AGENT`）；观察/对比，**不** auto-promote；失败/停滞仍归 T14 stall redispatch。见 [spec-p2](architecture/v8-upgrade/spec-p2.md)、[D8](architecture/v8-upgrade/issues/D8-coding-challenger.md)。 |
| Replan proposal only（V8 P3 / T18a） | **落地（默认关闭）** | Watchdog `replan` → 结构化 `ProposalKind.REPLAN`（`UC_REPLAN_PROPOSAL_ENFORCE`）；携带 `contract_digest` / `preserve_effects`；**不**执行新 DAG、**不** bump digest。真 replan execute 仍延后（trust-eval ticket 18）。见 [spec-p3](architecture/v8-upgrade/spec-p3.md)、[D9](architecture/v8-upgrade/issues/D9-replan-proposal-only.md)。 |
| Replan execute checklist（V8 T18b / D12） | **落地（默认关闭）** | Approve `replan` → Attention NeedsHuman 清单 + audit `replan_execute_checklist`（`UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE`）；**不** `admit_execution`、**不** bump digest、**不** 改 DAG。不满足 trust-eval ticket 18 的真 execute。见 [spec-t18b](architecture/v8-upgrade/spec-t18b.md)、[D12](architecture/v8-upgrade/issues/D12-replan-execute-checklist.md)。 |
| Durable replan accounting（V8 T18c / D13） | **落地（默认关闭）** | `WorkCase.replans_used` 权威计数（`UC_REPLAN_ACCOUNTING_ENFORCE`）；stall 忽略客户端影子；`replan` 裁决后 CAS +1；**不** bump digest、**不** 改 DAG、**不** `admit_execution`。配额前提，不满足 trust-eval ticket 18 的真 execute。见 [spec-t18c](architecture/v8-upgrade/spec-t18c.md)、[D13](architecture/v8-upgrade/issues/D13-replan-accounting.md)。 |
| Replan execute readiness（V8 T18d / D14） | **runbook + dry-run 落地** | 运维手册 + 非破坏探针：`docs/architecture/replan-execute-readiness-runbook.md`、`scripts/check-replan-execute-readiness.py`；报告 T18a–c / completion_conditions 前驱状态；**不**启用 execute、**不** bump digest、**不** `admit_execution`。不满足 trust-eval ticket 18。见 [spec-t18d](architecture/v8-upgrade/spec-t18d.md)、[D14](architecture/v8-upgrade/issues/D14-replan-execute-readiness.md)。 |
| MetaInfer kill/control 收据（V8 P6 / D16） | **落地（默认关闭）** | 第二个非 git kind `metainfer-kill`（`UC_EFFECT_LEDGER_METAINFER_CONTROL`）：终止 POST 丢响应 → `UNKNOWN`（不是不可判的「终止未确认」）；`CONFIRMED` 不重复 POST，`UNKNOWN` 拒绝重试直到对账；服务明确 `ok != true` → `FAILED`。运维对账（`inference/reconcile.py` 的 `attach` / `confirm_stop`）在证明远端状态后同时结算对应收据（T36），`FAILED` 不被证据翻案；待办队列可经只读端点 `GET /dashboard/api/effects`（默认 `UNKNOWN`，`status=RECONCILING&stale=1` 看租约过期的对账者）查看，该端点**不写**任何记录（D23）；Dashboard 的 #/dashboard/effects 视图把它呈现出来（默认 UNKNOWN，lease expired 过滤 RECONCILING），同样只读（D24）。本地 `write_file` 收据**不**进 ledger（证据面，另决策）。见 [spec-p6](architecture/v8-upgrade/spec-p6.md)、[D16](architecture/v8-upgrade/issues/D16-metainfer-control-receipt.md)。 |
| 本地工具收据（V8 P7 / D17） | **落地（审计面，无开关）** | attempt 级 `terminal.workspace`：变更文件（路径 + 变更类型，50 上限）+ 工具名（20 上限，去重），来自 `SubtaskResult.modified_files`（git diff）与 `recent_tool_calls`；**不**做逐工具拦截、**不**存 diff/内容、**不**新增存储；best-effort，写失败不影响子任务。见 [spec-p7](architecture/v8-upgrade/spec-p7.md)、[D17](architecture/v8-upgrade/issues/D17-local-tool-receipts.md)。 |
| watchdog 已确认副作用来源（V8 P8 / D18） | **落地** | 受治理 stall 时 worker 从 `tool_effects`（仅 `CONFIRMED`）派生 `confirmed_effect_ids`：按子任务分支（coding / inference 两种形式）与 MetaInfer `operation_id` 的 `<experiment_id>:` 前缀定域；`push-main` 收据记录它承载的 `uc/subtask/<id>` 分支集，因而也能按分支定域（D22/T39）；仍定域不到的记录（无分支集）留空——宁可少保护不假装保护。无 provider 时 payload 与旧行为逐字一致。见 [spec-p8](architecture/v8-upgrade/spec-p8.md)、[D18](architecture/v8-upgrade/issues/D18-preserve-effects-sourcing.md)。 |
| workload 角色与 report 边界（trust-eval P4 / D11） | **落地（默认关闭）** | role 上移到 workload：`BenchmarkSpec.eval_role` 为权威，runner 在成功与早退路径都盖章；`UC_EVAL_ROLE_SPLIT_ENFORCE` 下 `mint_report_result` 拒绝跨角色 mint，report remeasure 必须用 `report_benchmark`（缺失 fail-closed）。checkpoint 记录 `eval_role`，换角色恢复直接被拒、无 role 的旧 checkpoint 只允许 select 恢复（T27）。语料作者/存放仍属运维。见 [spec-p4](architecture/agent-trust-eval/spec-p4.md)、[D11](architecture/agent-trust-eval/issues/D11-workload-role-and-report-boundary.md)。 |
| 标注语料边界（trust-eval P5 / D12） | **落地** | 仓里只放 **manifest** 不放标注：`load_label_manifest` 要求人类 labeler（agent/worker/service 只能提议）、带 scheme 的仓外 `corpus_uri`、`source_eval_roles ⊆ {search, select}`（`report` 即 hold-out 泄漏而被拒）；CV artifact 带 manifest 加载时交叉校验 kind、`n_labels` 与 `cv_folds*2` 结构下限；`UC_REVIEWER_LABEL_PROVENANCE` 打开时注册也必须由 manifest 背书，否则该 kind 保持关闭（T28）。见 [spec-p5](architecture/agent-trust-eval/spec-p5.md)、[D12](architecture/agent-trust-eval/issues/D12-label-corpus-boundary.md)。 |
| 知识层：补丁链 / 证据账本 / 认知门 / 恢复视图（V2.0 波次） | **P0–P3 已接线（含 claim 生产者与晋升调用方）；P4 survey / deepen / pages 已落盘（自动 rebuild 已接在 arbiter canonical 晋升后，读面按生命周期聚合，页面不进 prompt）；P5 读取面 / 签名 / 指标 / 观测读面 / planner 预算开关已落盘**（17 个知识层聚焦测试文件、259 passed；22 个文件合计 374 passed；Dashboard 前端读面另有 7 个 node 测试，实测 2026-11-15） | 统一上下文底座（§6）之上补「一次 attempt 的局部经验如何变成共享知识」：`knowledge/model.py` 冻结词汇、`rollout.py` 分阶段开关默认关、`attempt_chain.py` 补丁链、`evidence.py` + `gate.py` 证据/校验/声明与 15 个拒绝理由、`recovery.py` 有界恢复视图（事实与假设分离）已接进 worker 重试环；worker 采集 attempt 结局/结构化 verdict/失败签名/合并与推送收据（默认 `visibility=attempt`），`claims.py` 生产失败与交付 claim，stage ≥ `graph` 时经门校验 PASS 后晋升 GRAPH；arbiter 在 stage `canonical` 用集成收据晋升 REPOSITORY；门裁决作为 `VALIDATION` 证据落库，`dashboard.py` 聚合 allowed/denied + 理由直方图。P5 读取面 `retrieval.py`（`UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS` 默认 500）、A-B 指标 `metrics.py` 与观测读面 `dashboard.py` 已落盘；P4 的 `survey.py` / `deepen.py` / `pages.py` 已落盘（`knowledge_pages` 命名空间，纯库能力，测试见 `test_knowledge_survey.py` / `test_knowledge_deepen.py` / `test_knowledge_pages.py`）；**仍缺**真实 A-B 数据集跑数与签名粒度校准；planner 视图预算已有 `UC_PLANNER_VIEW_BUDGET_BYTES`（T17，默认 8192、clamp 512–8192 的硬上限内可调）；Dashboard 前端 Knowledge 面板已接（读面 → 路由 → TaskDetail 面板）。见 §9、[knowledge-upgrade/map.md](architecture/knowledge-upgrade/map.md)、[spec-p0](architecture/knowledge-upgrade/spec-p0.md)、[D1](architecture/knowledge-upgrade/issues/D1-storage-authority.md)–[D7](architecture/knowledge-upgrade/issues/D7-page-projection-boundary.md)。 |
| Portable Capability Tokens | 不做（D4） | 继续 Grant/Mandate；MVP 不引入可移植不透明 token。 |
| Learning OS / 轨迹导出 | 延后（D3） | 产品决策前不做。 |
| Graph-primary 默认开启 | **runbook 落地，默认仍关**（D6 / D10 / P4） | 运维手册 + dry-run：`docs/architecture/graph-cutover-runbook.md`、`scripts/check-graph-cutover-readiness.py`；Compose/镜像默认仍关；不因 `UC_DATABASE_URL` 自动切 graph-only。真 replan execute 仍延后。 |

V8 MVP 波次已关闭（P0–P4 / D11）；T18 前驱波次已关闭（T18a–d / D15）；P6 MetaInfer kill/control 收据已落地（D16，默认关闭）；P7 本地工具收据已落到 Decision Record（D17，审计面）；P8 watchdog 的已确认副作用来源已接通（D18）；P9 Decision Record 的 Dashboard 只读 trail 已落地（D19），P10 起与 context manifest 同契约分页（D20），P11 起可按子任务收窄（D21）；真 replan execute 仍由 trust-eval ticket 18 拥有并延后。

（模型路由已于 P2 落地，见 §4；新增差距在此表登记，按优先级排序。）

## 6. Context Compiler（P1b，risk 配比 / manifest 审计 / 缓存亲和已落地）

`python/ultimate_coders/agent/context_compiler.py` 把「拼 prompt」变成可测试的
编译过程：

- **分层**：P0 契约/策略（永不因预算丢弃）、P1 当前状态与错误、P2 已验证证据、
  P3 执行轨迹、P4 长期记忆、P5 背景。P0 超预算时 `over_budget=True` 如实上报，
  而不是悄悄改掉任务约束。
- **统一预算**：依赖结果（P3）与检索命中（P2）竞争同一份 token 预算
  （`UC_CONTEXT_BUDGET_TOKENS`，默认 2000），取代过去「各自截断、总量无界」。
- **provenance**：每个 item 带 `source`/`source_version`/`observed_at`/
  `trust_class`/`verified`/`ttl_seconds`。`verified` 只表示某项具体校验通过，
  不表示内容为真；权限仍由工具网关独立裁决。
- **确定性**：排序不依赖输入顺序，同输入产生同输出；TTL 过期与内容重复
  都有显式丢弃原因，`summary()` 不含内容，可直接进事件与日志。
- **兼容渲染**：`render_legacy()` 与旧 `ContextInjector` 输出逐行一致
  （`tests/python/test_context_compiler.py` 以真实 injector 对比钉死），
  仅在预算真正丢弃内容时追加截断标记。
- **可观测**：`subtask_started` 事件携带 manifest 摘要（预算、各层计数、
  丢弃原因），排障时能看到「这次 prompt 为什么少了东西」。
- **审计落库**：同一份摘要同时写入 runtime state 的 `context_manifest`
  命名空间（`ContextManifestOutbox`，`python/ultimate_coders/context_outbox.py`），
  key 为 `task:subtask:attempt`，其中 attempt 是**派发尝试**
  （`dispatch_retry_count`，与 checkpoint / 终态投递同一身份），不是进程内
  重试计数。与终态 outbox「一个 subtask 一条」相反，manifest 是**按 attempt
  的快照**：重派发若重编译，两条都留存；同一 attempt 的重编译（重投递再
  执行）则替换该 attempt 的快照并**重记 `recorded_at`**——时间属于留下来的
  那份 summary（identical 重写是 no-op、保留原时间），所以 `recent()` 排的
  是内容时间而不是首次写入时间。曾经唯一的写入点把
  attempt 写死为 0，于是重派发覆盖掉第一次的快照、「按 attempt 留痕」在
  生产里从未发生（写入即投递，无重放
  状态机），best-effort——落库失败只记日志，不会让它描述的子任务失败。
  事件流丢失后，「这次为什么少了东西」仍可按 subtask 扫回
  （`tests/python/test_context_outbox.py`）。读取面：
  `GET /dashboard/api/context/manifests?task_id=…[&subtask_id=…]`（auth 与
  分页同其他 dashboard 端点；返回 summary 原文，不含 prompt 内容）。带
  `subtask_id` 看一个子任务的历代编译；只给 `task_id` 时跨该任务全部
  子任务扫回——单次编译的 key 自带 attempt，但跨子任务时纯字符串序会把
  `:10` 排在 `:2` 前，故按（子任务前缀, attempt 数值）重排；任务前缀
  带冒号，`task-1` 不会吞掉 `task-10`
  （`tests/python/test_dashboard_context_manifests.py`）。任务详情面板把
  这条痕迹渲染成可扫读的文本
  （`dashboard/src/components/panels/ContextManifestPanel.tsx`）：它取
  `order=recent` 的**最近窗口**——端点默认的 key 序是最旧窗口，面板若用
  默认值，截断时恰好藏掉审计要看的最近尝试——并按后端时间序原样渲染、
  不再自行重排；逐条显示编译时间（UTC）· subtask · attempt、预算/总量/
  稳定量、risk 与**按 tier 归因**的丢弃原因（summary 的 `dropped[].tier`
  与 `items_by_tier` 用同一套 tier 标题；旧审计记录里的数字 tier 降级显示
  `P3`，不编造标题）；渲染器只读
  summary 的已知字段，未知字段（哪怕混进了内容）永不输出
  （`dashboard/tests/contextManifests.test.mjs` 以带毒字段与顺序保持断言
  这一点）。窗口有剩余时计数行会如实说「N of M」，`order=recent` 还接受
  `before` 游标（上一页最后一行的 key，锚在总序里而非偏移，并发写入不会
  让翻页重复投递；响应带 `next_cursor`，短页为 null 表示到底），面板的
  「load more」据此续页、按 key 去重并保留已载入行；key 序不分页——它是
  另一个机制（key 序 LIMIT 窗口），`before` + `order=key` 明确 400 而不是
  假装成同一件事。每行的 subtask 是可点的 scope selector：点它进入该子任务
  的历史（`subtask_id` 过滤，翻页在同一 scope 内继续），「all subtasks」
  回到任务视图；scope 属于已载入状态的身份，切任务或切 scope 不会先闪出
  上一个窗口。
  gateway 组合块与本地依赖结果**都**进同一个 `compile_context` 并**都**落
  manifest（D2）：`from_gateway_context_block()`（trust `gateway_committed`）
  与 `from_dependency_results()`（trust `worker_reported`）只是不同的输入
  适配器，预算、去重、tier 归属与 summary 字段完全一致；
  `render_legacy(legacy_style="gateway")` 只决定标题与截断提示，不决定是否
  记录。「不编译所以不记录」的空历史已不存在——两条路径的 manifest 都按
  `task:subtask:dispatch_attempt` 落在同一条审计链上
  （`tests/python/test_context_block.py`、`tests/python/test_context_compiler.py`）。
  按时间读「最近 N 次编译」另有显式序：`order=recent`（或直接调
  `ContextManifestOutbox.recent()`）按 `recorded_at` 新在前返回。
  `RuntimeState.query` 的截断发生在 key 序（`ORDER BY record_key
  LIMIT n`），「取 N 条再按时间排」得到的只是「key 窗口里的最新」，
  key 靠后而时间最新的那条恰好会被截掉——正是该方法要放上来的那条；
  故 recent 用 key 游标扫完整个前缀后才按时间序截断，`limit` 只约束
  响应、不约束扫描；排序键 `(-recorded_at, key)` 全序，同刻按 key 升序
  （`tests/python/test_context_outbox.py`）。
  窗口与总数必须分开报：`history`/`task_history` 是**最旧 key 窗口**
  （截断丢的是最新那几条），响应里的 `count` 是窗口大小、`total` 是
  扫描计数——把窗口大小显示成「N recorded」会在恰好值得看的长痕迹上
  低估总数；面板计数行走 `manifestCountLine`（截断时显示「N of M」，
  缺 `total` 时降级不编造「of N」，
  `dashboard/tests/contextManifests.test.mjs`）。

预算配比已跟随路由风险落地：`compile_context` 在
`RISK_RESERVED_SHARE` 命中时（今天是 high）为 (P0, P2) 组——任务契约与
已验证证据——预留一份预算，P1 状态转储或执行轨迹不能再把它们挤掉；
预留是保底不是封顶，未用部分回流，延迟的证据仍按常规层级竞争剩余
预算；词汇表对齐 `routing.RiskLevel`，表外风险（含默认 `normal`）逐字节
恒等。worker 把 attempt-0 路由前移到编译之前，编译直接消费
`route.risk`，高风险子任务的 prompt 不再丢证据
（`tests/python/test_context_compiler.py` 的 `TestRiskAwareAllocation`
与 `tests/python/test_model_routing.py` 的
`TestWorkerContextRiskRouting`）。

- **缓存亲和**：`render_parts()` 把编译结果切成（稳定前缀, 易变尾部）。
  稳定标记由编译器**派生**而非调用方配置——policy 层（任务契约）在一个
  编译期内不变，天然是稳定前缀；其余 tier 保持调用方显式标记，未标记
  即易变（未听说 `stable` 的调用方逐字节不变；policy 层本就在 tier 序
  最前，打包序同样不变）。`summary()` 的 `stable_tokens` 给出可被
  cache-aware serving 栈复用的 prefill 量——这是可观测指标；**真正复用
  仍待推理侧消费该分段**（沙箱 agent 尚未接线），接口与度量先就绪
  （`tests/python/test_context_compiler.py` 的 `TestCacheAffinity`）。

- **统一入口**：两个上下文适配器之外，worker 只调用一次 `compile_context`。
  检索块（`from_search_block()`，P2 / `repository_search`）与记忆块
  （`from_hy_memory_block()`，P4 / `hy_memory_recall`）也走同一入口，因此
  任何一条 prompt 都能回答「它由哪些 item 组成、谁被丢弃、为什么」。

### 恢复视图（重试的第二次上下文）

`python/ultimate_coders/knowledge/recovery.py` 把「同一 subtask 链上最近几轮
补丁与结局」编译成一段**有界**文本，作为普通的 P2 证据项进入同一个编译器：

- 事实项 `recovery-facts:<attempt.key()>`：`trust_class="attempt_ledger"`、
  `verified=True`、`relevance=1.0`；假设项
  `recovery-hypotheses:<attempt.key()>`：`trust_class="unverified_hypothesis"`、
  `verified=False`、`relevance=0.6`——**模型的自述不因被渲染就变成事实**（原则 A）。
- 预算来自 `rollout.recovery_budget_tokens()`（`UC_RECOVERY_BUDGET_TOKENS`，
  默认 600，clamp 128–2000），字符预算 = `budget * 4`；新轮优先、装不下丢最旧
  并置 `truncated=True`，假设在预算竞争里先让位（原则 C：默认 attempt 局部）。
- worker 在 `attempt > 0` 且 stage ≥ `recovery` 时调用它，用**同一个**
  `compile_context` 重新编译整份 context（不是追加第二预算的文本），并沿用
  同一 manifest 键；`subtask_retry` 事件只带 `summary()`（轮数/预算/截断/
  tokens），不含补丁或失败原文（`python/ultimate_coders/agent/worker.py::
  _recovery_context_block`，`tests/python/test_recovery_view.py`）。

P1b 三项增量（统一预算、risk 配比、manifest 审计落库）连同本缓存亲和
接口已全部落地；路由（P2）见 §4；知识层的证据链与认知门见 §9。

## 7. 故障注入矩阵（F1–F8）与本仓库覆盖

| 编号 | 注入故障 | 现有证据 | 状态 |
|---|---|---|---|
| F1 | LLM 调用期间崩溃 | `test_unconfirmed_writer_preserves_checkpoint_and_original_baseline` | 覆盖 |
| F2 | 工具成功但响应丢失 | `test_lost_submission_response_never_creates_another_job`、去重集成测试、`tests/python/test_merge_arbiter_effects.py`、`tests/python/test_workspace_push_effects.py`（已推送但响应丢失 → `already_pushed`，不重试）、`tests/python/test_metainfer_effects.py`（`UC_EFFECT_LEDGER_METAINFER`：create POST 丢失 → `UNKNOWN`，CONFIRMED 不二次 POST；`UC_EFFECT_LEDGER_METAINFER_CONTROL`：kill POST 丢失 → `UNKNOWN`、`ok != true` → `FAILED`、CONFIRMED/UNKNOWN 均不二次 POST）、`tests/python/test_outbox_effect_identity.py`（重试只取代一条终态记录，消息 id 随内容变化） | 覆盖 |
| F3 | 沙箱/工作区被销毁 | inference delivery 的 worktree 接管与恢复测试 | 覆盖 |
| F4 | 外部工具返回恶意指令 | env allowlist 与凭证隔离测试；注入内容侧：毒化的检索块/子任务摘要只进证据/轨迹层、不能自证 verified、也不能伪造 policy 或 P3 计数（内容由 builder 决定信任，文本说了不算） | 覆盖（层内不变量；模型是否遵从注入不在仓内可控） |
| F5 | 两个 Agent 改同一资源 | `test_merge_arbiter.py`、`test_merge_gate.py` | 覆盖 |
| F6 | 执行中修改目标 | `tests/python/test_contract_versioning.py`（契约摘要变化 → 拒绝执行并报 `contract_changed`） | 覆盖 |
| F7 | 预算耗尽 | `tests/python/test_budget_ledger.py`（准入拒绝 → 不创建沙箱；账本保留，提高上限后更高 attempt 继续；未上报 usage 记 `unreported` 不记 0） | 覆盖 |
| F8 | Verifier 错误放行 | Oracle 拒绝噪声基准的测试；零 objective 基线不再能证明改进（即使没有相对门槛——此前 0 → 125 tokens/s 可被放行，百分比与噪声门槛都被跳过）；零候选 objective 测量也不再算无限改进（此前 tpot 43 → 0 被接受并报 100%）；零峰值内存 claim 不再能满足 memory 上限（约束是 claim-vs-ceiling，不是比较）；未武装指纹的基准 runner 拒绝测量（fail closed，而不是静默跳过校验） | 部分（harness 自报 correctness 的 attestation 仍待解决） |

F2 是生产级平台的必测项：它检验的是系统是否理解分布式执行，而不只是实现了
一个多轮模型循环。

## 8. 不变量

1. 恢复只读已提交的事实；LLM 的未提交推理不进入恢复路径。
2. 一个逻辑 effect 的所有重试共享同一身份；attempt 递增不得产生重复副作用。
3. P0 上下文不因预算被静默丢弃；丢弃必有原因且可观测。
4. 权限检查与内容可信度分离：上下文里的「高相关」不等于「有权限」。
5. 长期记忆永远不是运行状态的唯一依据；状态以记录为准。
6. KV Cache 与预填充优化不参与正确性；任何缓存丢失都必须可重建。
7. **证据、校验、声明三分离**：`PASS` 只把声明从 `CANDIDATE` 推到 `VALIDATED`，
   提升到更高可见性必须由认知门按固定理由词表裁决；提示词内容（prompt /
   messages / diff / patch / transcript）永不进入权威记录（D4）。
8. **知识层默认关闭且 fail-closed**：`UC_KNOWLEDGE_STAGE` 未设或无法识别即
   `DISABLED`，stage C 起才改变 prompt；读取按可见性 / 作用域 / 修订绑定裁决，
   目标是「陈旧 claim 误注入 = 0」「未授权提升 = 0」（D5）。
9. **补丁轮次按 attempt 身份不可变**：同一 `AttemptRef.key()` 同内容重放是
   no-op，不同内容必须抛冲突而不是覆盖；符号定位 AST 优先、失败回退行号且
   永不因解析失败丢整条证据（D3）。
10. **知识页是可重建投影，不是第二权威**：`knowledge_pages` 只住 RuntimeState，
   可由 claim/证据确定性重建；页面不进 prompt、不同步 Qdrant/TiKV、不参与
   Stage A–E，消费方必须 scope 隔离 + 修订过滤 + 只读 `to_summary()` 级字段（D7）。

## 9. 知识层与证据链（V2.0 波次）

本节只描述已落盘的实现与明确的缺口；阶段表、四原则、验收指标与评测协议以
[knowledge-upgrade/spec-p0.md](architecture/knowledge-upgrade/spec-p0.md) 为唯一
权威，决策见 [D1–D6](architecture/knowledge-upgrade/issues/D1-storage-authority.md)。

### 9.1 记录模型（一个事实一个权威）

| 记录 | 命名空间 | 身份 | 关键点 |
|---|---|---|---|
| 补丁轮次 `AttemptRound` | `attempt_chain` | `attempt:{task}:{subtask}:{dispatch}:{attempt}` | 每轮一个不可变记录：outcome、patches（路径/变更类型/增删行/裁剪 diff/符号）、base/head revision、summary/failure/test_verdict；同键同内容幂等、不同内容抛 `AttemptChainConflictError` |
| 证据 `EvidenceRecord` | `knowledge_evidence` | `{attempt.key()}:{evidence_id}` | `evidence_id` 由（kind, attempt, revision, payload）摘要派生；`trust_class`/`verified` 由 kind 默认值决定，不由文本自称 |
| 校验 `ValidationRecord` | 随 claim 记录 | `short_digest(claim_id, result, revision, strategy, …)` | 幂等；首次 PASS 推进 status，后续 FAIL 不回退已通过的校验 |
| 声明 `ClaimRecord` | `knowledge_claims` | `short_digest({scope, statement.casefold()})` | 生命周期 `CANDIDATE → VALIDATED → PROMOTED`，另有 `SUPERSEDED` / `RETRACTED` 终态；`human_locked` 优先 |

三张表都在既有 `runtime_state.py` 的 `mutate`/`versioned_get` 上实现
（PostgreSQL 集群 / SQLite 本地），**没有**第二套权威存储：Rust 侧 Knowledge
Authority 不在 MVP 内，未来若上移也只是把同一组语义搬到网关，而不是新增
一写者（D1）。Qdrant / TiKV / 版本化知识页是可重建投影，读服务不得反写。

### 9.2 认知门（Epistemic Gate）

`python/ultimate_coders/knowledge/gate.py` 是纯函数，读与晋升都返回
`GateDecision(allowed, visibility, reasons)`：

- **读**：`ATTEMPT` 只允许同一 attempt 链（`same_chain=True`）；`GRAPH` 要求
  `graph_id` 与租户/仓库/分支都匹配；`REPOSITORY` 要求作用域可读且
  `RevisionBinding.applies_to(reader_revision)`（空绑定是通配，短 SHA 前缀
  需 ≥7 位且无歧义）。
- **晋升**：目标可见性必须更高、非终态、`attempt_succeeded` 且非 stale、
  存在同修订的 PASS 校验；`ATTEMPT → GRAPH` 还要 `graph_id`，
  `GRAPH → REPOSITORY` 还要集成收据（`integration_ref` + `integration_sha`）
  与已知推送状态（`UNKNOWN` 不算成功）。
- **拒绝理由词表固定**：`target_not_higher` / `attempt_not_successful` /
  `attempt_stale` / `validation_missing` / `validation_failed` /
  `validation_inconclusive` / `revision_mismatch` / `graph_scope_missing` /
  `integration_missing` / `push_unknown` / `claim_terminal` /
  `cross_scope_denied` / `graph_not_shared` / `attempt_chain_required` /
  `stage_disabled`——指标与审计按这套键聚合，不按自然语言。
- 账本入口 `EvidenceLedger.promote()` 在 CAS 里重新求值同一门，拒绝时
  **不改记录**；stage 为 `DISABLED` 时记 `stage_disabled`。

### 9.3 放量（Stage A–E）

| Stage | `UC_KNOWLEDGE_STAGE` | 行为 |
|---|---|---|
| A | 未设 / `disabled` | 关闭；默认值，未知值也落在这里（fail-closed） |
| B | `shadow`（`1`） | 只采集补丁/证据，prompt 不变 |
| C | `recovery`（`2`/`canary`） | 重试注入有界恢复视图（本波次 MVP 的唯一行为开关） |
| D | `graph`（`3`） | 图内共享**读面已接**：worker 在 `subtask_started` 前读本 task 图内已晋升 claim 注入 `Tier.EVIDENCE`（T14）；同时允许把本 attempt 的交付 claim 校验 PASS 后晋升 GRAPH |
| E | `canonical`（`4`） | MergeArbiter 用集成收据（`integration_ref` / `integration_sha`）把仓库级 claim 晋升 REPOSITORY（T12），并在本批有晋升时重建仓库页（T18 `_rebuild_pages`，页面是读模型投影，不参与放量门、不进 prompt） |

`rollout.describe_stage()` 可在启动日志里打印一次；预算与规模上限也在
`rollout.py`（`UC_RECOVERY_BUDGET_TOKENS` 600、`UC_ATTEMPT_CHAIN_MAX_ROUNDS` 20、
`UC_KNOWLEDGE_MAX_PATCH_FILES` 20、`UC_KNOWLEDGE_MAX_DIFF_CHARS` 20000、
`UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS` 500），
任何数字都不在调用点重复。planner 侧视图预算在 Rust gateway 侧
（`crates/uc-types/src/envelope.rs`）：`UC_PLANNER_VIEW_BUDGET_BYTES` 默认
`CONTEXT_BLOCK_MAX_BYTES`（8192），clamp 512–8192，1500–1800 tokens ≈ 6–7.2 KiB
落在可调区间（T17）。

### 9.4 采集点与读面（worker / orchestrator 侧全部 stage-gated 且 best-effort；metrics 为离线）

| 钩子 | 时机 | 写什么 |
|---|---|---|
| `worker.py::_record_attempt_round` | 沙箱返回后、release/merge 之前（worktree 仍持有补丁） | `AttemptRound`：outcome、patches（路径/变更类型/增删行/裁剪 diff/符号）、base→head 修订；`test_verdict` 来自 `_verification_report()` 的紧凑标签（无结构化校验时仍是 `"unknown"`——轮次不替校验下结论） |
| `worker.py::_record_attempt_evidence` | 紧接其后 | `ATTEMPT_OUTCOME`（success/retryable/cleanup_pending/attempt/retry_count/changed_files 计数）+ 结构化 `REVIEW_VERDICT` / `ORACLE_VERDICT` / `TEST_VERDICT`（仅在 `SubtaskResult.review` / `domain_result` 真的有裁决时）+ 失败时 `FAILURE_SIGNATURE`（`signature` 为归一化签名 ≤200 字符、`excerpt` 为原文 ≤500）与失败 claim |
| `worker.py::_record_delivery_evidence` / `_record_delivery_claim` | workspace release 之后 | `MERGE_RECEIPT`（status/commit_sha/branch_preserved）与 `PUSH_VERDICT`（push_status，`UNKNOWN` 原样保留）；`visibility=attempt` 的交付 claim，stage ≥ `graph` 且 merge 被接受时校验 PASS 并晋升到 GRAPH（失败 attempt 不晋升） |
| `merge_arbiter.py::_record_integration` / `_rebuild_pages` | 基分支 push 之后 | 每个合并分支的 `MERGE_RECEIPT` + `PUSH_VERDICT`（`visibility=repository`）；stage `canonical` 且 scope 有 graph_id 时写仓库级 claim，校验 PASS 后以 `integration_ref/integration_sha` 晋升 REPOSITORY（`UNKNOWN` 推送被 `push_unknown` 拒绝）；晋升后按 claim + 集成 binding 幂等重建仓库页（T18，投影失败只计 `pages/errors`，不影响合并） |
| `worker.py::_load_recovery_view` / `_recovery_context_block` | 重试环 `attempt > 0`、重新路由之后 | 读本链 `prior_rounds()` → 有界恢复视图 → 与首次上下文一起重新编译；`subtask_retry.data["recovery"]` 只带计数 |
| `worker.py::_knowledge_context_items` | 每次 attempt 组上下文之前（`subtask_started` 发射前） | stage ≥ `graph` 时按 `KnowledgeScope(graph_id=subtask.parent_id)` 读本图内**已晋升** claim，经 `claims_to_context_items` 以 `Tier.EVIDENCE` 注入；`subtask_started.data["knowledge"]` 只带 claims 数/tokens（不含 statement） |
| `knowledge.metrics::main` | 事后离线（非运行期） | 从 `attempt_chain` + `knowledge_evidence` 采集样本，算 success_rate / avg_attempts / repeated_failure_rate / tokens，并按 task 配对比较 baseline/variant；`--signatures` 额外输出归一化失败签名直方图（校准粒度用，不是判优依据）；CLI：`python -m ultimate_coders.knowledge.metrics --state <sqlite-or-pg-url> [--baseline f.json --variant g.json] [--json] [--signatures]`；跑数步骤见 [knowledge-upgrade/runbook-ab-evaluation.md](architecture/knowledge-upgrade/runbook-ab-evaluation.md) |
| `claims.record_gate_decision`（门裁决落证据） | 每次 `validate_and_promote_delivery` / `promote_failure_claim` / arbiter 晋升之后 | `VALIDATION` 证据：`allowed` / 有界 `reasons` / `target` / `claim_id` / `strategy`，**不含 claim statement**；证据 id 由 payload 派生，重放同一裁决不虚增 |
| `knowledge.dashboard::main` | 事后只读（非运行期） | 有界概览：attempt 结局、证据 by kind/visibility、claim by status、门裁决 allowed/denied + 理由直方图、知识页生命周期计数；CLI：`python -m ultimate_coders.knowledge.dashboard --state <sqlite-or-pg-url> [--task-id T] [--json]` |
| `dashboard/app.py::knowledge_api` | 请求时只读（Dashboard 读面） | `GET /dashboard/api/knowledge?task_id=&limit=`：与 manifests / decisions 同一 `_check_auth`（远程需 token，localhost 直通）；`limit` 1..5000，非法 400；委托 `aggregate_knowledge`，状态不可用时 503 |
| `uc-types/src/envelope.rs` planner 预算 | gateway 组 `ContextBlock` 时（运行期，非知识采集） | `UC_PLANNER_VIEW_BUDGET_BYTES`（默认 `CONTEXT_BLOCK_MAX_BYTES` 8192，clamp 512–8192）经 `planner_view_budget_bytes()` 进 `compose_with_budget`；非法/空缺回默认，超上限被降回硬上限；溢出只丢整 entry 并置 `truncated`（T17） |
| `dashboard/src/components/panels/KnowledgeOverviewPanel.tsx` | 页面渲染时只读（前端读面） | TaskDetail 内挂载；`dashboard/src/lib/knowledge.ts` 只读已知计数字段（attempts/evidence/claims/pages、门 allowed/denied + 前 3 条理由、`truncated`），空数据显式说明「本任务未采集」；**不渲染 claim statement / diff / page body**；7 个 node 测试 |

所有证据身份仍按 attempt 归属；stage A 全部 no-op，stage B 只采集，
stage C 让恢复视图进入 prompt，stage D 让图内读面与本图晋升生效，stage E 再加
arbiter 的 REPOSITORY 晋升与仓库页重建（版本化知识页仍属读模型投影，不进 prompt）。
聚焦测试（2026-11-15 实测 **17 个知识层测试文件、254 passed**；24 个相关知识层 /
worker 文件合计 **382 passed**）：
`test_knowledge_model.py`、`test_rollout.py`、`test_attempt_chain.py`、
`test_evidence_ledger.py`、`test_epistemic_gate.py`、`test_knowledge_loop.py`
（证据 → 声明 → 校验 → 晋升的安全核心端到端验收：未校验不可晋升、失败 attempt
不可晋升、`UNKNOWN` 推送不算成功、拒绝不改记录、重放幂等）、
`test_knowledge_claims.py`、`test_verdicts.py`、`test_recovery_view.py`、
`test_workspace_patch_capture.py`、`test_worker_attempt_evidence.py`、
`test_worker_delivery_verdict.py`、`test_merge_arbiter_knowledge.py`、
`test_failure_signatures.py`、`test_knowledge_retrieval.py`、
`test_knowledge_metrics.py`、`test_knowledge_dashboard.py`、
`test_knowledge_survey.py`、`test_knowledge_deepen.py`、`test_knowledge_pages.py`。

### 9.5 未接线或未验收（诚实缺口）

- **真实 A-B 数据集**（stage B vs C 的配对比较）未跑：`knowledge/metrics.py`
  + CLI 已就绪（T15，判定词表 `improved` / `non_inferior` / `regressed` /
  `inconclusive` / `insufficient_data`，`--signatures` 输出失败签名直方图），
  并配了可执行的 [runbook](architecture/knowledge-upgrade/runbook-ab-evaluation.md)
  （臂的开法、状态库隔离、样本量与两轮同向规则）；但还没有在真实任务集上产出
  结论——重复失败签名率、平均 attempt 数、成功率、tokens/task 的下降/非劣结论
  全部待测，数据出来前不得对外宣称改善。
- **planner 侧视图预算（T17 已接开关，硬上限未放开）**：D6 的 1500–1800 tokens
  现由 Rust gateway 的 `UC_PLANNER_VIEW_BUDGET_BYTES` 调节
  （`crates/uc-types/src/envelope.rs`：默认 `CONTEXT_BLOCK_MAX_BYTES` 8192，clamp
  512–8192，非法值回默认）；这是环境开关而非第二套常量，A-B 只需改 env。
  `cargo test -p uc-types envelope` 需在 CI 跑（本沙箱离线，仅用独立 rustc
  验证了纯函数逻辑），未被 CI 执行前视为未验收。
- **Dashboard 读面与前端面板均已接**：`knowledge/dashboard.py`（T16）提供
  有界只读概览与 CLI（门裁决 allowed/denied + 理由直方图、证据/claim/页面计数），
  `GET /dashboard/api/knowledge` 路由已在 `dashboard/app.py` 注册并有 4 个测试
  （鉴权、limit 校验、schema、内容不外泄）；`dashboard/src/lib/knowledge.ts` +
  `components/panels/KnowledgeOverviewPanel.tsx` 在 TaskDetail 内渲染该概览
  （counts-only，空状态显式说明，7 个 node 测试，`tsc -b` / `eslint` 干净）。
- **版本化知识页（P4）**：`knowledge/survey.py`（AST survey）、
  `knowledge/deepen.py`（有界只读 git deepening）与 `knowledge/pages.py`
  （`knowledge_pages` 命名空间、生命周期 active→superseded/retracted、
  `rebuild()` 幂等、locked 拒绝自动改写）均已落盘并有聚焦测试；自动 rebuild
  触发方已接在 arbiter 的 canonical 晋升之后（T18 `integration._rebuild_pages`，
  以 claim + 集成 binding 重建仓库页，投影失败只计 `pages/errors`），T16 读面按
  生命周期聚合页面计数；页面**尚未进入 prompt**（仍属读模型投影）——该边界已由
  [D7](architecture/knowledge-upgrade/issues/D7-page-projection-boundary.md) 定案：
  不进 prompt、不同步 Qdrant/TiKV、不占 Stage 门，消费面只有读面与将来的 planner
  视图。
- **签名粒度校准**：`knowledge/signatures.py` 已做归一化（去 ANSI、
  traceback 取末尾错误行、路径/UUID/hex/数字占位、≤200 字符，全噪声返回空串），
  `metrics.py --signatures` 已能观测分布；但「同一签名」的粗细度仍需在 A-B 数据
  里校准。
- **已接线（不再是缺口）**：claim 生产者（失败 claim + 交付 claim）、晋升调用方
  （worker 的 GRAPH 晋升、arbiter 的 REPOSITORY 晋升）、图内读面
  （`worker.py::_knowledge_context_items`）与仓库页自动重建
  （`integration._rebuild_pages`，T18）均已落盘，planner 视图预算亦有
  `UC_PLANNER_VIEW_BUDGET_BYTES`（T17），见 §9.4 与
  `tests/python/test_knowledge_claims.py`、`test_worker_attempt_evidence.py`、
  `test_merge_arbiter_knowledge.py`、`test_knowledge_retrieval.py`、
  `test_knowledge_pages.py`。

2026-11-15 复测：`python -m compileall` 干净、`ruff` 干净；24 个相关知识层 / worker
测试文件 **382 passed**（其中 17 个知识层聚焦文件 254 passed；读面子集 59 passed）；
Dashboard 侧 `tsc -b` / `eslint` 干净，`node --test tests/*.test.mjs` **170 passed**
（其中 7 个为知识层读面前端）。Rust 侧 `cargo test -p uc-types envelope` 因离线
未能执行（见 §9.5 与 T17）。

