# Spec P0 — 基座：统一 Context Compiler 与冻结词汇

Blocked by: 无（D1 / D2 / D5 / D6 已决定）。本规格是整条知识升级波次的基座，
同时承载六模块 / 六阶段 / 四原则 / 验收指标与评测协议的权威表。

## Goal

把「上下文」从各调用点各自拼装，收敛成一个可测试、可审计、可分期放量的编译面：
冻结证据/声明/可见性的词汇，定义 A–E 五级 rollout 与默认关闭，让 worker 的
两条上下文来源都经过同一个 `compile_context` 并留下同一份 manifest。本阶段
**不改变 prompt 内容**（默认 stage A），只提供后续 P1–P3 的共同语言与开关；
P4/P5 是 Frontier（见 [map.md](map.md)），本轮不写规格文件。

## 六模块与六阶段

| 阶段 | 模块（用户计划编号） | 本波次交付 | Rollout stage | 规格 |
|---|---|---|---|---|
| P0 | ① 统一 Context Compiler | `knowledge/model.py` 词汇 + `knowledge/rollout.py` 开关 + `context_compiler.py` 适配器与 token meter 注册表 + worker 双路径统一编译/落库 | A（默认关）/ B（只采集） | 本文件 |
| P1 | ② Attempt Diff-Chain | `knowledge/attempt_chain.py`（`AttemptChainLedger` / `AttemptRound` / `FilePatch` / `SymbolChange` / `parse_git_diff` / `locate_symbols`）+ 采集适配器 `WorkspaceManager.capture_diff()` / `head_sha()` | B `SHADOW`（只采集，prompt 逐字不变） | [spec-p1.md](spec-p1.md) |
| P2 | ③ Evidence Ledger + ④ Epistemic Gate（安全面同阶段交付） | `knowledge/evidence.py`（证据/声明/校验记录与幂等账本）+ `knowledge/gate.py`（`can_read` / `evaluate_promotion`） | B `SHADOW`（记录与判定只落日志；C 起才拒绝） | [spec-p2.md](spec-p2.md) |
| P3 | ⑥ 的 MVP 切片：有界 Recovery View + Integration Receipt | `knowledge/recovery.py`（`build_recovery_view` / `recovery_context_items`）+ worker 重试环注入 + 合并/推送收据入证据账本 | C `RECOVERY`（首次改 prompt） | [spec-p3.md](spec-p3.md) |
| P4 | ⑤ Versioned Knowledge Pages（Git Deepen / AST survey） | 已落盘：`knowledge/survey.py`（AST survey）、`knowledge/deepen.py`（有界只读 git deepening）、`knowledge/pages.py`（`knowledge_pages` 命名空间 / 生命周期 / 幂等 `rebuild()`）；知识页只从权威派生、可重建，自动 rebuild 已接在 arbiter canonical 晋升后（T18），页面仍不进 prompt | 不参与放量门（读模型投影） | [tickets/18](tickets/18-page-rebuild-trigger.md) |
| P5 | ⑥ Budgeted Retrieval / Views（图共享读取面、按需检索、Dashboard、评测面板） | 已落盘：`knowledge/retrieval.py`（图内读面 + 预算化打包，`UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS` 默认 500）、`knowledge/signatures.py`（失败签名归一化）、`knowledge/metrics.py`（A-B harness + CLI，含 `--signatures` 失败签名直方图）、`knowledge/dashboard.py`（有界观测读面：门裁决 allowed/denied + 理由直方图、证据/claim/页面计数）、`GET /dashboard/api/knowledge` 与 TaskDetail 内的 React 面板（`KnowledgeOverviewPanel.tsx`，counts-only）；worker 在 stage ≥ `graph` 注入已晋升 claim；planner 视图预算开关 `UC_PLANNER_VIEW_BUDGET_BYTES`（Rust `ContextBlock`，默认 8192、clamp 512–8192，T17）。**仍缺**：真实 A-B 数据集跑数与签名粒度校准（runbook 见 [runbook-ab-evaluation.md](runbook-ab-evaluation.md)） | D `GRAPH` + T17 | [tickets/17](tickets/17-planner-view-budget.md) |

> 阶段号与模块号不是一一对应：P2 同时承载模块③④（gate 是 ledger 的安全面，
> 不单独成阶段），P3 只取模块⑥的 MVP 切片（恢复视图），完整模块⑥在 P5。
> stage 顺序 A<B<C<D<E 与阶段顺序对齐到 P3：`C` 是恢复视图，`D`（graph 共享）
> 随 P5 读取面启用，`E`（canonical）驱动 arbiter 的 REPOSITORY 晋升——P4/P5 的
> 先后是文档与风险顺序，不是 stage 顺序；P4 知识页是读模型投影，不占 stage。
> **任何 stage 未显式设置时一律 `DISABLED`（D5）。**

## 权威存储与默认关闭（D1 / D5 摘要）

- 知识层的唯一权威是既有 `RuntimeState`（`python/ultimate_coders/runtime_state.py`）：
  `UC_DATABASE_URL` 非空时走 PostgreSQL，本地回退到 `UC_RUNTIME_STATE_DIR`
  （默认 `.uc/runtime`）下的 `state.sqlite3`。命名空间：
  `attempt_chain`、`knowledge_evidence`、`knowledge_claims`。
- **不新增 Rust Knowledge Authority 服务**，也不引入第二套权威存储；Qdrant /
  TiKV / 未来的知识页都是可重建投影，任何投影都不得反写权威（D1、
  [issues/D1-storage-authority.md](issues/D1-storage-authority.md)）。
- 所有行为开关默认 OFF：`UC_KNOWLEDGE_STAGE` 未设 = `DISABLED`，未知值
  fail-closed；`UC_RECOVERY_BUDGET_TOKENS` 默认 600、`UC_ATTEMPT_CHAIN_MAX_ROUNDS`
  默认 20、`UC_KNOWLEDGE_MAX_PATCH_FILES` 默认 20、`UC_KNOWLEDGE_MAX_DIFF_CHARS`
  默认 20 000（默认值只住 `knowledge/rollout.py`）。
- **测试随 ticket 交付**：每个 ticket 都必须在 `tests/python/test_*.py` 里新增或
  扩展聚焦用例；没有测试的 ticket 视为未交付。

## 四原则及其落点

| 原则 | 含义 | 本波次的落点 |
|---|---|---|
| **A. 事实 ≠ 解释** | 补丁、结果、校验是事实；根因猜测、摘要、模型自述是解释 | `ContextItem.verified` 语义（只表示某项具体校验通过）；`recovery_context_items()` 把事实与假设拆成两个 item；`EvidenceKind.ORACLE_VERDICT/TEST_VERDICT` 是事实，`HYPOTHESIS` 不是 |
| **B. 校验 ≠ 发布** | 通过校验不等于可以变成共享事实 | `ValidationResult.PASS` 只推进 `ClaimStatus.VALIDATED`；提升还需 `EpistemicGate.evaluate_promotion()` 的可见性/修订/集成/推送条件（D4） |
| **C. attempt 局部 ≠ 长期** | 默认只在同一条重试链内可见 | `Visibility.ATTEMPT → GRAPH → REPOSITORY`；`EpistemicGate.can_read()`；恢复视图只读同一 subtask 链（P3） |
| **D. 存储权威 ≠ 检索投影** | 一个事实只有一个权威，投影可重建 | `RuntimeState`（`attempt_chain` / `knowledge_evidence` / `knowledge_claims`）是权威；Qdrant / TiKV / 知识页是投影，不得反写（D1） |

## What to build

1. **冻结词汇（`python/ultimate_coders/knowledge/model.py`）**：
   `AttemptRef`（`task:subtask:dispatch:attempt` 身份 + `order()`）、
   `Visibility`（attempt/graph/repository + rank）、`EvidenceKind` +
   `DEFAULT_EVIDENCE_TRUST` / `VERIFIED_EVIDENCE_KINDS`、`ClaimStatus`、
   `ValidationResult` + `validation_result_passed()`、`KnowledgeScope`
   （tenant/repo/branch/graph + `allows_read()`）、`RevisionBinding`
   （pin revision / `applies_to()`）、`canonical_json` / `digest` /
   `short_digest` / `safe_text`。全部 import-light，不依赖运行时。
2. **分阶段开关（`knowledge/rollout.py`）**：`UC_KNOWLEDGE_STAGE`
   （A/off/disabled/空 → `DISABLED`；B/shadow；C/recovery/canary；D/graph；
   E/canonical；也接受 0–4），未知值 fail-closed 到 `DISABLED`；
   `capture_enabled` / `recovery_enabled` / `graph_sharing_enabled` /
   `canonical_enabled` / `describe_stage()`；资源上限
   `UC_ATTEMPT_CHAIN_MAX_ROUNDS`（默认 20）、`UC_KNOWLEDGE_MAX_PATCH_FILES`
   （默认 20）、`UC_KNOWLEDGE_MAX_DIFF_CHARS`（默认 20_000）、
   `UC_RECOVERY_BUDGET_TOKENS`（默认 600，clamp 128–2000）。
3. **统一编译面（`agent/context_compiler.py`）**：新增适配器
   `from_gateway_context_block()` / `from_search_block()` / `from_hy_memory_block()`
   （`from_dependency_results()` 已在），使两条来源都只产生 `ContextItem`；
   预算/去重/risk 预留/打包仍只在 `compile_context()` 里做。可插拔 token
   计量：`DEFAULT_TOKEN_METER = "heuristic"`、`register_token_meter(name, meter)`、
   `resolve_token_meter(name=None) -> (key, meter)`（未知名字抛 `ValueError`，
   不静默回退）、`ContextManifest.token_meter` 记录本次使用的 meter，
   `compile_context(..., token_meter=None)` 透传——真实 tokenizer 由部署方注册，
   本波次只钉住接口与 manifest 字段。
4. **worker 双路径（`agent/worker.py::_execute_subtask`）**：gateway 组合块与本地
   依赖注入器二选一喂入同一次 `compile_context`；`render_legacy(legacy_style=…)`
   保持两种历史渲染逐字兼容；两条路径都设置 `self._last_context_manifest` 并调用
   `_record_context_manifest()`（D2）。
5. **射程内的审计面**：`context_manifest` 落库 key 仍是派发身份
   `task:subtask:attempt`（`context_outbox.py`）；`subtask_started` 事件继续携带
   manifest 摘要。
6. **阶段表和指标表**：本文件 §验收指标与 §评测协议是整条波次唯一权威；tickets /
   map 只链接不复述数字。

## Acceptance criteria

- [x] `knowledge/model.py` 的 `normalize_*` 对未知取值 fail-closed（抛
      `ValueError`），布尔/空串别名归一化有单测；`short_digest` 长度 clamp、
      `safe_text` 去 NUL 且截断带标记。
- [x] `rollout.resolve_stage()` 未知值一律 `DISABLED`；`stage_from_env()` 用同一
      解析；四个能力谓词与 `describe_stage()` 一致；所有默认值只住 `rollout.py`
      （clamp 边界由单测钉死）。
- [x] 两条上下文来源都经过 `compile_context`：gateway 路径也写 manifest
      （`tests/python/test_context_outbox.py` 断言 gateway 路径有记录）。
- [x] `render_legacy` 与旧 `ContextInjector` 输出逐行一致，只有真正丢弃内容时
      追加截断标记（`tests/python/test_context_compiler.py`、
      `tests/python/test_context_block.py`）。
- [x] 适配器（`from_gateway_context_block` / `from_search_block` /
      `from_hy_memory_block`）只产出 `ContextItem`，信任类与 tier 有断言
      （`tests/python/test_context_compiler.py`）。
- [x] `register_token_meter()` / `resolve_token_meter()` 注册表可测：未知 meter
      抛 `ValueError`、默认 `heuristic`、`ContextManifest.token_meter` 与
      `summary()` 字段一致（`tests/python/test_context_compiler.py`）。
- [x] 权威存储未新增：记录落既有 `RuntimeState` 命名空间（`UC_DATABASE_URL`
      优先 PostgreSQL，否则 `UC_RUNTIME_STATE_DIR/state.sqlite3`），无新服务、
      无第二套存储（D1）。
- [x] `UC_CONTEXT_BUDGET_TOKENS` 默认 2000 未被本波次改变
      （`worker.py::_context_budget_tokens`）。
- [x] 默认 stage 为 `DISABLED`：默认部署下新代码只增加可观测性，不改变 prompt 与
      调度行为。

## 验收指标（用户计划的指标表）

| 指标 | 定义 | 目标（实验起点，非承诺） | 采集点 |
|---|---|---|---|
| 任务成功率 | 同一任务集 end-to-end 通过率 | 非劣于基线；stage C 后开始要求提升 | benchmark 结果 + `terminal` 事件 |
| 平均 attempt 数 | 每个成功任务的平均 attempt | 下降（MVP 主指标） | `decision_record` / attempt 链 |
| 重复失败签名率 | 相邻 attempt 失败签名相同的比例 | 下降（`FAILURE_SIGNATURE` 证据可计算） | 证据账本 `failure_signature` |
| tokens/task | 端到端 token 用量 | 非劣化护栏（不以砍证据换 token） | `budget.py` 用量 + manifest |
| compile P95 | `compile_context` 端到端耗时 P95 | 非劣化；超阈值先查 token meter | `manifest.summary()` / 事件时间 |
| 证据采集完整度 | 每次 attempt（含失败/超时/崩溃）都有 `AttemptRound` 的比例 | 100% | `attempt_chain` 命名空间 |
| stale-memory 误注入率 | 已过期/已撤回/修订不匹配的 claim 进入 prompt 的次数 | **0** | `EpistemicGate.can_read()` 拒绝原因 |
| 未授权提升数 | 未满足 gate 条件却变成 GRAPH/REPOSITORY 可见的 claim 数 | **0** | `knowledge_claims` 的 `promote()` 拒绝日志 |

## 评测协议

- **同一任务集、同一模型与采样参数、同一 token 预算、同一沙箱镜像**；唯一变量是
  知识升级的 stage。
- **对照组**（按序）：① 当前系统（stage A，等价于无本波次行为）；② stage B
  （只采集，prompt 逐字不变——用于证明「采集本身不改变结果」）；③ stage C
  （attempt 2+ 读有界 Recovery View）。P4/P5 之后再加 ④ 自然语言摘要视图、
  ⑤ 原始 HyCodeMem 召回、⑥ 增强 Recovery View（结构化 + 证据 + 假设分离）。
- **判读规则**：以「下游任务结果」为主指标（成功率、attempt 数、重复失败签名率）；
  检索命中率（retrieval hit rate）与压缩率（compression ratio）**不能单独用来
  判优**——它们可以解释差异，但不能替代任务结果。
- **配对比较**：同一任务在基线与实验组配对，报成功/失败与 attempt 差值的分布，
  而不是两个单点均值；两轮重复取一致方向。
- **测量实现**：`python/ultimate_coders/knowledge/metrics.py`（T15）已实现
  `collect_samples()` / `compute_metrics()` / `compare_metrics()`（判定词表
  `improved` / `non_inferior` / `regressed` / `inconclusive` / `insufficient_data`，
  token 容差 `TOKEN_TOLERANCE=0.05`），`--signatures` 额外输出归一化失败签名
  直方图（签名粗细度校准的观测面），CLI：
  `python -m ultimate_coders.knowledge.metrics --state <sqlite-or-pg-url>
  [--baseline f.json --variant g.json] [--json] [--signatures]`。跑数步骤、样本量
  与判读顺序见 [runbook-ab-evaluation.md](runbook-ab-evaluation.md)。**真实数据集
  跑数未做**——harness 就绪不等于结论成立。
- **失败判读**：stage C 若不优于 stage B，不得进入 stage D/E；stage B 即便指标变差
  也说明采集有副作用，应先查 manifest 与 prompt 字节一致性。

## MVP 闭环

1. Attempt 1：`WorkspaceManager.capture_diff()` 在 sandbox 返回后、`release()`
   之前捕获补丁事实 → `AttemptChainLedger.record()` 写一条不可变 `AttemptRound`。
2. Attempt 1 失败后，Attempt 2 读**有界** Recovery View（默认
   `UC_RECOVERY_BUDGET_TOKENS=600`）：只含同一 subtask 链的补丁/结局/校验事实 +
   未验证假设。
3. 度量：重复失败签名率是否下降、平均 attempt 数是否下降、成功率是否非劣。
4. 只有闭环通过，才允许把可见性放宽到 graph 共享与长期知识页（P4/P5）。

## Out of scope (P0)

- 补丁链 / 证据 / 声明的实际记录（P1–P2）。
- 任何 prompt 内容变化（stage A/B 不变；C 起才由 P3 提供）。
- 知识页 Pages（P4，`survey.py` / `deepen.py` / `pages.py` 已落盘；自动
  rebuild 已作为 T18 接在 arbiter canonical 晋升后，页面仍不进 prompt）与
  planner 侧视图预算开关（T17：`UC_PLANNER_VIEW_BUDGET_BYTES`，Rust
  `ContextBlock` 的既有预算，默认 8192、clamp 512–8192）；图共享读取面已作为
  P5 切片落盘（`knowledge/retrieval.py`，T14），本轮不为其单写规格。
- 把 `TaskBudgetLedger`（`budget.py`）与 prompt 预算合并。
- Rust Knowledge Authority 与任何第二套权威存储（D1 明确不做）。
