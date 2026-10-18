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

## 5. 差距（按优先级）

| 缺口 | 现状 | 升级方向 |
|---|---|---|
| **Effect Ledger 的 UNKNOWN 语义** | outbox 只有 pending/delivered；推送/合并失败一律视为可重试 | 引入 `PREPARED → IN_FLIGHT → CONFIRMED`，网络超时进入 `UNKNOWN` 并**先对账再重试**；按 attempt 分键改为按逻辑 effect 分键 |
| **Context Compiler** | `ContextInjector` 仅按字符截断，无来源、无预算、无丢弃记录 | 本 PR 落地第一切片：分层（P0–P5）+ 统一 token 预算 + provenance + 兼容渲染（见 §6） |
| **Progress Detector** | retry 只看次数与错误类型 | 以「新信息/证据增量」判定无效循环，连续同错触发 replan |
| **任务契约版本化** | 目标在任务中途变化无显式语义 | 契约带版本，变更需重新验收（F6） |
| **预算与配额** | 无任务级预算账本 | 预算耗尽停止新执行并保存状态（F7） |
| **模型路由** | 每阶段固定 agent/模型 | 按阶段与风险选择模型与推理预算；失败分类决定是否值得再调模型 |

## 6. Context Compiler（P1b，第一切片已落地）

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

后续增量：risk 相关的预算配比（高风险管理任务给 P0/P2 更高保底）、manifest
持久化到 outbox 供事后审计、稳定前缀/易变尾部的缓存亲和（`render_parts()` 已
给出结构，等待推理侧消费）。

## 7. 故障注入矩阵（F1–F8）与本仓库覆盖

| 编号 | 注入故障 | 现有证据 | 状态 |
|---|---|---|---|
| F1 | LLM 调用期间崩溃 | `test_unconfirmed_writer_preserves_checkpoint_and_original_baseline` | 覆盖 |
| F2 | 工具成功但响应丢失 | `test_lost_submission_response_never_creates_another_job`、去重集成测试 | 覆盖 |
| F3 | 沙箱/工作区被销毁 | inference delivery 的 worktree 接管与恢复测试 | 覆盖 |
| F4 | 外部工具返回恶意指令 | env allowlist 与凭证隔离测试 | 部分（注入内容本身未测） |
| F5 | 两个 Agent 改同一资源 | `test_merge_arbiter.py`、`test_merge_gate.py` | 覆盖 |
| F6 | 执行中修改目标 | — | 缺失 |
| F7 | 预算耗尽 | — | 缺失 |
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
