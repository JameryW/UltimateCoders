# Wayfinder Map: Knowledge Upgrade（V2.0 证据优先的知识层）

Status: active — **P0–P3 全部实现且已接线**，P4（survey / deepen / pages / 自动
rebuild）与 P5 切片（失败签名归一化 / 图共享读取面 / A-B 指标 harness / 知识层读面 /
planner 视图预算开关）已落盘；
2026-11-15 实测 17 个知识层聚焦测试文件 **259 passed**（22 个相关知识层 / worker
文件合计 **374 passed**，`compileall` 与 `ruff` 干净）。P2 账本/门与证据采集已接进 worker，claim 生产者（失败 / 交付）与晋升
调用方（worker → GRAPH、arbiter → REPOSITORY）已接线，结构化 review/oracle/test
裁决会进入证据与 `AttemptRound.test_verdict`；P3 恢复视图与合并/推送收据已接线；
门裁决（允许/拒绝 + 理由码）作为 `VALIDATION` 证据落库，可由读面聚合。
Dashboard 读面从模块 → CLI → `GET /dashboard/api/knowledge` → React 面板
（`dashboard/src/lib/knowledge.ts` + `components/panels/KnowledgeOverviewPanel.tsx`，
TaskDetail 内挂载，7 个 node 测试，`tsc -b` / `eslint` / 170 个 dashboard node 测试全过）
已闭环。P4 页面有了自动重建触发方（`integration.record_integration` 的 canonical
路径按提升后的 claim 重建仓库页，页面仍是读模型投影、不进 prompt）；planner 视图预算
有了 env 开关（`UC_PLANNER_VIEW_BUDGET_BYTES`，Rust `ContextBlock` 硬上限内）。
指标 harness 新增 `--signatures` 失败签名直方图（把签名粒度校准从「不可见」变成
「可观测的分布」），并配了可执行的 A/B runbook
（[runbook-ab-evaluation.md](runbook-ab-evaluation.md)）；page 边界（不进 prompt /
不同步 Qdrant/TiKV / 不占 Stage）由 D7 定案。2026-11-15 首轮真实小样本已跑通
（3 个任务对，usage 上报、配对与签名直方图均工作），但两臂 attempt 0 全成功，
stage C 未注入，结果不构成判读（runbook §7）；同一天的失败注入补齐了 stage C
验证（C 的 recovery 摘要递增、B 为 `None`，runbook §8）。随后又用
`max_turns=1` 注入带 usage 的失败混合样本，验证签名归一化（`<n>`）、
配对判词与 `verdict` 计算能走通，但该轮 `regressed` 是注入方式的伪信号
（runbook §9），仍不作为 B/C 性能结论。2026-11-15 又用 AwesomeWorkflows
裁剪出的真实 2 步工作流任务跑通 B/C：真实 worktree/commit/attempt_chain、
确定性产物验收（doc/report）与 paired `non_inferior` 均工作；但两条配对任务
在 C 臂 attempt 0 都成功，恢复视图未注入，token 差异只是模型波动
（runbook §10），同样不是判读数据。
仍在 Frontier：决策级 B/C 数据（成功/失败混合、两轮同向）与签名粒度校准（工具与
runbook 均已就绪，缺的是数据）、Rust 侧 `cargo test`（本机网络不可达；
CI `test (uc-types)` job 已加，待首次绿色）。
Assessment: [assessment.md](assessment.md)（surveyed 2026-11-15；现状盘点 / 六模块
差距 / 方案取舍 / 风险）
Intake: 用户计划的「六模块 / 四原则 / 六阶段 / Stage A–E 放量 / 验收指标 /
评测协议」+ 与既有 Decision Record、Effect Ledger、Hy-Memory 的边界

本波次要解决的是一句话：**重试为什么还在犯同样的错，以及一次 attempt 的局部
经验凭什么变成共享知识。** 范围严格限定为「证据优先的知识层」：补丁事实链、
证据/声明账本、认知门、有界恢复视图。它不重写 Context Compiler 的表达策略、
不改 Memory 的读写协议、不引入新的权威存储；默认全部关闭（`UC_KNOWLEDGE_STAGE`
未设 = `DISABLED`），每一阶段都能单独回滚。规格与阶段表见
[spec-p0.md](spec-p0.md)（唯一权威：六阶段 / 四原则 / 指标 / 评测协议）。

## Destination

同一任务集上，stage B（只采集）→ stage C（重试读有界恢复视图）的配对比较显示：
**重复失败签名率与平均 attempt 数下降、成功率非劣、tokens/task 不劣化**；
且「陈旧 claim 误注入 = 0」「未授权提升 = 0」两个安全门槛始终成立。达标后才允许
把可见性放宽到图共享（P5 / stage D）与仓库级晋升（stage E）；版本化知识页（P4）
是读模型投影，另行按 Frontier 推进，不参与 Stage A–E 放量门。指标定义与
目标见 [spec-p0.md](spec-p0.md#验收指标用户计划的指标表)。

## Notes

- **Domain docs** — 状态权威与不变量见
  [../../agent-os-architecture.md](../../agent-os-architecture.md)（§2 权威表、
  §8 不变量）；组件边界见 [../../architecture.md](../../architecture.md)。
- **ADR / 决策** — D1–D6 见 [Decisions so far](#decisions-so-far)；与 v8 的关系：
  v8 解决「决定与收据可审计」，本波次解决「证据与知识可晋升」，两者共享
  RuntimeState + Decision Record 的身份约定，但**不合并存储**（D1）。
- **Tracker mode** — 本地 Markdown（本目录）；票在
  [tickets/](tickets/)，每个票必须有 `tests/python/test_*.py` 的聚焦用例。
- **Standing preferences** — 中文行文、英文标识符；数字必须引用 owning 常量/env
  var；不发明未落盘符号；缺什么写「Phase X / not yet wired」，不写「应该」。
- **用户计划锚点** — 六模块 / 六阶段表、四原则、验收指标与评测协议全部以
  [spec-p0.md](spec-p0.md) 为唯一权威（`## 六模块与六阶段`、
  `## 四原则及其落点`、`## 验收指标（用户计划的指标表）`、`## 评测协议`、
  `## MVP 闭环`）；Stage A–E 的运行语义与 env var 见
  [../../agent-os-architecture.md](../../agent-os-architecture.md) §9.3。
- **相关波次** — [../v8-upgrade/map.md](../v8-upgrade/map.md)（Decision Record /
  收据）、[../agent-trust-eval/map.md](../agent-trust-eval/map.md)（评测方法；
  本波次的评测协议沿用其配对比较纪律）。

## Decisions so far

- [D1 存储权威只在 RuntimeState，不新增 Rust Knowledge Authority](issues/D1-storage-authority.md)：
  一个事实一个权威；Qdrant / TiKV / 知识页只是可重建投影，不反写。
- [D2 gateway 组合块也走统一编译器并落 manifest](issues/D2-unified-compile-surface.md)：
  两条上下文来源同一预算、同一 manifest，消除「不编译所以不记录」的空历史。
- [D3 补丁轮次按 attempt 身份不可变幂等，符号定位 AST 优先](issues/D3-patch-round-identity.md)：
  同键同内容重放、不同内容抛冲突；`.py` 用 dotted symbol，其余行号回退。
- [D4 证据 / 校验 / 声明分离，晋升由门裁决且理由词表固定](issues/D4-evidence-claim-separation.md)：
  PASS 只到 `VALIDATED`；图级/仓库级晋升要成功 attempt + 同修订校验 + 集成收据 +
  已知推送状态；15 个 `REASON_*` 是指标键。
- [D5 放量分 A–E 五级、默认全关、未知值 fail-closed](issues/D5-rollout-stages.md)：
  `UC_KNOWLEDGE_STAGE` 未设 = `DISABLED`；stage C 起才改 prompt。
- [D6 token 预算是有界实验起点，数字只住 rollout.py](issues/D6-token-budgets.md)：
  恢复视图默认 600（`UC_RECOVERY_BUDGET_TOKENS`），planner 视图有 env 开关
  `UC_PLANNER_VIEW_BUDGET_BYTES`（默认 8192 = `CONTEXT_BLOCK_MAX_BYTES` 硬上限，
  clamp 512–8192，1500–1800 tokens 落在可调区间）；预算区间是实验起点而非承诺；
  指标以任务结果为主，不以命中率/压缩率单独判优。
- [D7 知识页是只读投影：不进 prompt、不同步向量库、不占 Stage 门](issues/D7-page-projection-boundary.md)：
  `knowledge_pages` 权威仍在 RuntimeState 且可确定性重建；prompt 知识面只有
  `retrieval.py` 的已晋升 claim 与 `recovery.py` 的恢复视图；收窄 D5 第 4 条中
  「仓库级知识页进入 prompt」的措辞；页面消费方必须 scope 隔离 + 修订过滤 +
  `to_summary()` 级字段。

## Implementation backlog

P0（D1–D2、D5–D6）— 统一上下文底座与冻结词汇: **implemented**
- Spec: [spec-p0.md](spec-p0.md)
- [01 冻结知识层词汇与规范化](tickets/01-model-vocabulary.md) — **implemented**
  (`knowledge/model.py`; `tests/python/test_knowledge_model.py`)
- [02 分阶段开关与预算默认值](tickets/02-rollout-stages.md) — **implemented**
  (`knowledge/rollout.py`；`UC_KNOWLEDGE_STAGE` / `UC_RECOVERY_BUDGET_TOKENS` 等；
  `tests/python/test_rollout.py`)
- [03 统一编译面与 worker 双路径](tickets/03-unified-compile-surface.md) —
  **implemented** (`context_compiler.py` 适配器 + token meter；`worker.py`
  双路径；`test_context_compiler.py` / `test_context_block.py` /
  `test_context_outbox.py`)

P1（D3）— Attempt Diff-Chain: **implemented**
- Spec: [spec-p1.md](spec-p1.md)
- [04 补丁链记录与 diff 解析](tickets/04-attempt-diff-chain.md) — **implemented**
  (`knowledge/attempt_chain.py`; `tests/python/test_attempt_chain.py`)
- [05 AST 优先的符号定位](tickets/05-symbol-location.md) — **implemented**
  (`locate_symbols`；同测试文件)
- [06 补丁采集适配器与 worker 接线](tickets/06-patch-capture-adapter.md) —
  **implemented** (`workspace.py::capture_diff/head_sha`；
  `worker.py::_record_attempt_round`；`test_workspace_patch_capture.py` 4 passed)

P2（D4）— Evidence Ledger 与 Epistemic Gate: **implemented + wired**
- Spec: [spec-p2.md](spec-p2.md)
- [07 证据 / 校验 / 声明账本](tickets/07-evidence-ledger.md) — **implemented**
  (`knowledge/evidence.py`；`tests/python/test_evidence_ledger.py`)
- [08 认知门与固定拒绝词表](tickets/08-epistemic-gate.md) — **implemented**
  (`knowledge/gate.py`；`tests/python/test_epistemic_gate.py`)
- [09 证据采集接线与聚焦测试](tickets/09-evidence-wiring.md) — **implemented**
  (`worker.py::_record_attempt_evidence` / `_verification_report` /
  `knowledge/verdicts.py`；`tests/python/test_worker_attempt_evidence.py` 12 tests，
  含结构化 `REVIEW_VERDICT` / `ORACLE_VERDICT` / `TEST_VERDICT` 与失败 claim；
  `test_verdicts.py` 18、`test_knowledge_claims.py` 17)
- [09b claim 生产者与晋升调用方](tickets/09-evidence-wiring.md) — **implemented**
  (`knowledge/claims.py`；worker 在 stage ≥ `graph` 时校验 PASS 并晋升交付 claim，
  失败 attempt 不晋升；`tests/python/test_worker_attempt_evidence.py`)

P3（D4、D6）— Recovery View 与 Integration Receipt: **implemented**（版本化知识页见 P4）
- Spec: [spec-p3.md](spec-p3.md)
- [10 有界恢复视图：事实与假设分离](tickets/10-recovery-view.md) —
  **implemented** (`knowledge/recovery.py`；`test_recovery_view.py` 11 passed)
- [11 重试环注入与可观测](tickets/11-recovery-injection.md) — **implemented**
  (`worker.py::_load_recovery_view` / `_recovery_context_block`；
  `subtask_retry.data["recovery"]` 在重试决策点计算；端到端 A-B 验收未做)
- [12 合并 / 推送收据入证据账本](tickets/12-integration-receipt.md) —
  **implemented** (`worker.py::_record_delivery_evidence` 记录
  `MERGE_RECEIPT` / `PUSH_VERDICT`；`merge_arbiter.py::_record_integration` 在
  orchestrator 角色的合并侧为每个合并分支写仓库级收据，stage `canonical` 时以
  `integration_ref/integration_sha` 晋升 REPOSITORY；`UNKNOWN` 推送被
  `push_unknown` 拒绝；`tests/python/test_merge_arbiter_knowledge.py` 16 tests)

P5（D4、D6 的读取面切片）— Graph 检索 / 签名 / 指标 / 读面 / planner 预算: **implemented**
- Spec: [spec-p0.md](spec-p0.md)（P5 切片）+ [spec-p3.md](spec-p3.md)
- [13 失败签名归一化](tickets/13-failure-signatures.md) — **implemented**
  (`knowledge/signatures.py`；worker 失败路径 payload 带归一化 `signature` 与原文
  `excerpt`；`tests/python/test_failure_signatures.py` 9 tests)
- [14 图共享读取面与按预算检索](tickets/14-graph-retrieval.md) — **implemented**
  (`knowledge/retrieval.py`；`UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS` 默认 500；
  worker `_knowledge_context_items` 在 stage ≥ `graph` 时读取并注入
  `Tier.EVIDENCE`；`tests/python/test_knowledge_retrieval.py` 10 tests +
  worker 注入用例)
- [15 端到端 A-B 指标测量](tickets/15-metrics-harness.md) — **implemented**
  (`knowledge/metrics.py` + CLI，含 `--signatures` 失败签名直方图；
  `tests/python/test_knowledge_metrics.py` 15 tests；跑数 runbook 见
  [runbook-ab-evaluation.md](runbook-ab-evaluation.md)；已有两轮真实小样本
  验收（成功集 §7、失败注入 §8），决策级数据仍属 Frontier)
- [16 知识层读面：概览与拒绝理由分布](tickets/16-knowledge-read-face.md) —
  **implemented** (`knowledge/dashboard.py` + CLI；门裁决写入 `VALIDATION` 证据后
  聚合为 allowed/denied + 理由直方图；页面按生命周期计数；读面同时是 P4 页面投影
  的消费面；`tests/python/test_knowledge_dashboard.py` 17 tests，含 `GET /dashboard/api/knowledge` 路由测试)
- [17 planner 视图预算开关](tickets/17-planner-view-budget.md) — **implemented**
  (`crates/uc-types/src/envelope.rs`：`UC_PLANNER_VIEW_BUDGET_BYTES` 默认
  `CONTEXT_BLOCK_MAX_BYTES`（8192），clamp 512–8192；`ContextBlock::compose` 走
  `compose_with_budget`；Rust 单测覆盖解析/clamp/小预算打包)

P4 — Versioned Knowledge Pages: **implemented**
- `knowledge/survey.py`（AST survey）、`knowledge/deepen.py`（有界只读 git
  deepening）、`knowledge/pages.py`（`knowledge_pages` 命名空间 / 生命周期 /
  幂等 `rebuild()` / locked 拒绝自动改写）；`tests/python/test_knowledge_survey.py`、
  `test_knowledge_deepen.py`、`test_knowledge_pages.py`。知识页是读模型投影，
  不参与 Stage A–E 放量门；读面（T16 `dashboard.py`）会按生命周期聚合它们的数量。
- [18 页面自动重建触发方](tickets/18-page-rebuild-trigger.md) — **implemented**
  (`knowledge/integration.py::_rebuild_pages`：arbiter 在 stage `canonical` 且本批
  有 REPOSITORY 晋升时，用提升后的 claim + 集成收据 binding 重建仓库页；投影失败
  只计入 `pages/errors`，不影响合并；`tests/python/test_merge_arbiter_knowledge.py`
  含重建/幂等/失败隔离用例)。页面**不进 prompt**：prompt 可见的知识仍走 claim 检索面。

## Frontier (open, unblocked)

- **真实 A-B 数据集**：T15 的测量脚本、判定词表、`--signatures` 直方图与可执行
  runbook（[runbook-ab-evaluation.md](runbook-ab-evaluation.md)）都已就绪；
  2026-11-15 已在真实 worker + Ollama 上跑通一轮小样本（3 个任务 × 2 arm × 1
  attempt），但该轮 `verdict=inconclusive` / `tokens_worse`、两臂签名均为空，
  且没有触发 stage C 的恢复视图（attempt 0 全成功，成功结果不重跑）。同一天的
  失败注入另一轮验证了 stage C：两臂各 3 次失败 attempt，C 的 `subtask_retry`
  事件带回递增的 recovery 摘要（1→2 轮、95→184 token），B 为 `None`（runbook
  §8）。随后 `max_turns=1` 的带 usage 失败混合轮验证了判读管线本身
  （`paired_tasks=2`、签名 `<n>` 聚合、`verdict=regressed`），但该判词是注入
  伪信号，不是 stage 结论（runbook §9）。2026-11-15 又从 AwesomeWorkflows 裁剪
  真实 2 步工作流语料（doc/report）跑通 B/C：真实 worktree/commit/attempt_chain、
  确定性产物检查和 `paired_tasks=2` / `non_inferior` 可用，但两条配对任务的
  attempt 0 都成功，恢复视图未注入，token 差异是模型波动；且同一路径
  “brief.md” 会跨任务覆盖，需按任务分目录或在完成时立即验收（runbook §10）。
  可判读的 B/C 数据仍缺：需要正常 `max_turns`、成功/失败混合、失败 attempt
  两臂配对，并按 runbook 跑两轮同向。
- **签名粒度校准**：T13 已归一化，`--signatures` 已能观测分布；首批真实样本
  没有失败 attempt（空直方图），失败注入轮出现单一连接错误签名，`max_turns`
  轮出现 `Stopped after max turns (<n>) without done()`，都说明当前归一化在
  这些类别上可行但粒度仍偏粗，仍需在正常任务集的混合失败样本里校准。
- **Rust 侧构建验证**：`crates/uc-types` 的 planner 预算改动在本机无法 `cargo test`
  （crates.io 网络不可达且无本地 registry 缓存）；已用独立 `rustc` 提取验证
  解析/clamp/打包逻辑。`.github/workflows/ci-rust.yml` 已新增 `test (uc-types)`
  job（`cargo test -p uc-types`），首次绿色运行前仍视为未验收。

已定案、不再列为缺口：知识页与 Qdrant/TiKV 的投影关系、页面是否进 prompt、
页面是否占 Stage 门 —— 由 [D7](issues/D7-page-projection-boundary.md) 收口
（页面只读投影、不进 prompt、不同步向量库、不占 Stage）。

## Out of scope

- Rust Knowledge Authority 服务、第二套权威数据库（D1）；知识层不参与
  `CONTRACT_VERSION` lockstep。
- 重写 Context Compiler 的分层/预算/渲染策略；本波次只统一入口与证据输入。
- 替换或改写 Hy-Memory / 分层 Memory 协议：本波次只给「晋升进长期记忆」加门，
  不动其存储与召回实现。
- 模型训练、prompt 模板重设计、评测集扩容。
- 跨租户合规策略扩展与知识页保留期策略。
