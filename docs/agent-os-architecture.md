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
| 长期记忆 | Memory 层 / hy-memory | **不作为**恢复依据 |
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
  已落地则报 `already_pushed` 而非重试。
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
  不决定「改做什么」——本仓库没有 replan 原语，编造一个只是装饰。
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

（模型路由已于 P2 落地，见 §4；本表现在为空——新增差距在此登记，按优先级排序。）

## 6. Context Compiler（P1b，risk 配比与 manifest 审计落库已落地）

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
  key 为 `task:subtask:attempt`。与终态 outbox「一个 subtask 一条」相反，
  manifest 是历史快照：重试若重编译，两条都留存；写入即投递（无重放
  状态机），best-effort——落库失败只记日志，不会让它描述的子任务失败。
  事件流丢失后，「这次为什么少了东西」仍可按 subtask 扫回
  （`tests/python/test_context_outbox.py`）。gateway 组合块路径不本地
  编译，因而不留记录——空历史而非伪造记录。

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

后续增量：稳定前缀/易变尾部的缓存亲和（`render_parts()` 已给出结构，
等待推理侧消费）。

## 7. 故障注入矩阵（F1–F8）与本仓库覆盖

| 编号 | 注入故障 | 现有证据 | 状态 |
|---|---|---|---|
| F1 | LLM 调用期间崩溃 | `test_unconfirmed_writer_preserves_checkpoint_and_original_baseline` | 覆盖 |
| F2 | 工具成功但响应丢失 | `test_lost_submission_response_never_creates_another_job`、去重集成测试、`tests/python/test_merge_arbiter_effects.py`、`tests/python/test_workspace_push_effects.py`（已推送但响应丢失 → `already_pushed`，不重试）、`tests/python/test_outbox_effect_identity.py`（重试只取代一条终态记录，消息 id 随内容变化） | 覆盖 |
| F3 | 沙箱/工作区被销毁 | inference delivery 的 worktree 接管与恢复测试 | 覆盖 |
| F4 | 外部工具返回恶意指令 | env allowlist 与凭证隔离测试 | 部分（注入内容本身未测） |
| F5 | 两个 Agent 改同一资源 | `test_merge_arbiter.py`、`test_merge_gate.py` | 覆盖 |
| F6 | 执行中修改目标 | `tests/python/test_contract_versioning.py`（契约摘要变化 → 拒绝执行并报 `contract_changed`） | 覆盖 |
| F7 | 预算耗尽 | `tests/python/test_budget_ledger.py`（准入拒绝 → 不创建沙箱；账本保留，提高上限后更高 attempt 继续；未上报 usage 记 `unreported` 不记 0） | 覆盖 |
| F8 | Verifier 错误放行 | Oracle 拒绝噪声基准的测试 | 部分 |

F2 是生产级平台的必测项：它检验的是系统是否理解分布式执行，而不只是实现了
一个多轮模型循环。

## 8. 不变量

1. 恢复只读已提交的事实；LLM 的未提交推理不进入恢复路径。
2. 一个逻辑 effect 的所有重试共享同一身份；attempt 递增不得产生重复副作用。
3. P0 上下文不因预算被静默丢弃；丢弃必有原因且可观测。
4. 权限检查与内容可信度分离：上下文里的「高相关」不等于「有权限」。
5. 长期记忆永远不是运行状态的唯一依据；状态以记录为准。
6. KV Cache 与预填充优化不参与正确性；任何缓存丢失都必须可重建。
