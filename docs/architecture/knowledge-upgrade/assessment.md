# Assessment — V2.0 证据优先的知识升级

Surveyed: 2026-11-15（与本波次 P0–P3 代码同批）
Related: [map.md](map.md)、[spec-p0.md](spec-p0.md)、v8 收据/决策记录
[../v8-upgrade/map.md](../v8-upgrade/map.md)、信任评测
[../agent-trust-eval/map.md](../agent-trust-eval/map.md)

本文件回答一个问题：**在已有 Context Compiler / Decision Record / EffectLedger /
Hy-Memory 之上，为什么还需要「补丁链 + 证据账本 + 认知门 + 恢复视图」这一层？**
结论先说：现有系统已经能记录「一次 attempt 发生了什么」，但**不能回答「为什么这次
重试又犯了同样的错」**，也没有任何机制把「一次 attempt 的局部经验」安全地升级成
「可共享的事实」。缺口不在采集能力，而在**证据的身份、可见性与晋升规则**。

## 现状盘点

| 能力 | 现有实现 | 测试 | 与六模块的关系 |
|---|---|---|---|
| 上下文编译 | `agent/context_compiler.py`（tier / 预算 / risk 预留 / manifest / 缓存亲和）、`context_outbox.py`、dashboard `context/manifests` 端点与面板 | `test_context_compiler.py`、`test_context_outbox.py`、`test_context_block.py`、`dashboard/tests/contextManifests.test.mjs` | 模块①的地基；但 gateway 组合块路径此前不编译、不落 manifest（D2 修正） |
| 决策审计 | `decision_record.py`：attempt 级 route / context 指针 / terminal verdict，`decision_record` 命名空间 | `test_decision_record.py` | 只回答「这次尝试选了什么、结果如何」；**不含证据与声明** |
| 副作用账本 | `effect_ledger.py`：PREPARED→IN_FLIGHT→{CONFIRMED,FAILED,UNKNOWN}，push/merge 收据 | `test_merge_arbiter_effects.py`、`test_workspace_push_effects.py`、`test_metainfer_effects.py` | 证明「事实来自系统而不是模型自述」的先例；其 UNKNOWN≠FAILED 纪律被知识层继承 |
| 记录基座 | `runtime_state.py`：namespace + 版本 CAS，SQLite/PostgreSQL 同构 | `test_runtime_state.py` | 知识层权威的唯一载体（D1） |
| 工作区 / 补丁 | `agent/workspace.py`：worktree 租约、merge-base 与推送；本波次新增 `capture_diff()` | `test_workspace.py`、`test_workspace_patch_capture.py`（新） | 模块②的采集端；此前补丁只作为 git 事实存在，没有结构化的 attempt 记录 |
| 失败信号 | `progress.py`、`stall_redispatch.py`、`completion_conditions.py` | `test_progress*.py`、`test_stall_redispatch.py` | 失败「有没有发生」已知；失败「是不是同一个」没有签名可算 |
| 长期记忆 | `agent/hy_memory.py`（`CodingMemory.remember/recall`、`UC_HY_MEMORY_AUTO_CONTEXT`） | `test_hy_memory_*.py` | 已是「可写的长期事实」，但**写入无证据门**；只做了 supersede，没有声明层 |
| 分层 Memory | `memory/memory.py`（ShortTerm/LongTerm，经 engine → TiKV/Qdrant）、`search/query.py`、`agent/search_cache.py` | `test_memory_*.py`、`test_search_*.py` | 检索投影，非权威（D1）；检索命中≠可注入 |
| 预算 | `budget.py`（`UC_TASK_TOKEN_BUDGET` / `_COST_` / `_TIME_` 准入） | `test_budget_ledger.py` | 任务级准入预算，与 prompt 编译预算是两层（D6） |
| 代码结构 | `agent/codegraph.py`（`CodegraphClient.search` / sqlite 图） | `test_codegraph*.py` | 模块⑤（AST survey / 知识页）的候选数据源，本波次不动 |
| 回滚/灰度 | `routing.py` risk、`UC_*` env 开关惯例 | `test_model_routing.py` | 模块①的分阶段开关沿用同一惯例（D5） |

## 六模块差距

1. **统一 Context Compiler**：编译面已存在，但「gateway 组合块不编译」使 manifest
   存在空历史；且没有把「失败恢复视图」建模成普通编译输入。→ P0 修前者，P3 补后者。
2. **Attempt Diff-Chain**：没有 attempt 级补丁记录；重试只拿到自由文本
   `last_failure`，无法回答「上一轮改了什么、失败在哪、是不是同一个坑」。→ P1。
3. **Evidence Ledger**：Decision Record 记录终态，但证据（补丁 / 校验 / 失败签名）
   与声明（claim）没有结构化的、可幂等重放的身份。→ P2。
4. **Epistemic Gate**：没有任何可见性/晋升模型。记忆一旦写入即可被召回，
   「通过校验」与「可以共享」被混为一谈。→ P2（与 Evidence Ledger 同阶段交付）。
5. **Versioned Knowledge Pages**：知识页尚不存在；长期记忆是扁平事实，
   没有版本、修订绑定、AST 定位与 Git Deepen 深度。→ P4（已落盘：
   `survey.py` / `deepen.py` / `pages.py`；消费面仍属 Frontier）。
6. **Budgeted Retrieval / Views**：只有一个编译预算常量，没有按视图分预算的检索面；
   重试视图不存在。→ P5（图共享读取面已落盘，T14；Dashboard 读面与 React 面板已接）
   与 P3 的 MVP 切片（恢复视图）。

## 方案取舍

- **不新建权威存储**（D1）：`RuntimeState` 已有版本化 CAS 与双数据库同构；新增
  Rust Knowledge Authority 会让知识契约与 `CONTRACT_VERSION` 一样需要 lockstep，
  在量级未验证前是净负债。投影（Qdrant/TiKV/Pages）保持可重建。
- **不先做「更聪明的摘要 / 压缩」**：用户计划明确「不能只用检索命中率或压缩率判优」。
  先让事实可追（补丁链/证据），再谈表达（视图/知识页）；否则压缩掉的是唯一证据。
- **不把每次尝试的全部轨迹塞进 prompt**：恢复视图默认 600 tokens
  （`UC_RECOVERY_BUDGET_TOKENS`），事实与假设分离（原则 A）；无界注入会把
  「有界证据」变回「上下文洪水」。
- **不在 prompt 里放模型自述当事实**：`ContextItem.verified` 只表示某项具体校验
  通过；假设永远以 unverified 项进入（`recovery_context_items()` 拆两个 item）。
- **不把 gate 做成第二个权限系统**：gate 只决定「哪些 claim 可以进 prompt / 可以
  晋升」，实际权限仍由 sandbox 工具网关独立裁决（沿用 §8 不变量 4）。

## 主要风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 陈旧 claim 被注入 | 重试继承错误结论 | `RevisionBinding.applies_to()` + `EpistemicGate.can_read()`；指标「stale 误注入 = 0」 |
| 账本膨胀 | RuntimeState 体积、事件膨胀 | `MAX_PATCH_FILES` / `MAX_DIFF_CHARS` 截断并置 `truncated=True`；日志安全 `.summary()` |
| 写入路径把 prompt 内容带进权威 | 审计泄漏 | 只存 log-safe JSON；`summary()` 不含内容；沿用 Decision Record 的 no-content 纪律 |
| attempt 身份错配 | 幂等失效、重复记录 | 统一 `AttemptRef`（dispatch_attempt 与 attempt 分开）与 `attempt.key()`；`record()` 冲突抛 `AttemptChainConflictError` 而不是覆盖 |
| 失败/成功都写不全 | 证据采集完整度 <100% | capture 只在 sandbox 返回后、`release()` 之前，不依赖模型自述；超时/崩溃路径也要写（P1 接线核对） |
| 评测被模型/任务集变化混淆 | 结论不可归因 | 固定模型/预算/镜像 + 配对比较 + stage B 作为「只采集不改 prompt」的对照 |
| rollout 误开 | 生产行为突变 | 默认 `DISABLED`，未知值 fail-closed（D5） |

## 代码落地状态与已解决的不一致（2026-11-15 复核）

盘点时发现的三处「文档/代码不一致」已全部由实现方解决，本波次文档按解决后的
事实书写：

1. **gateway 组合块路径不编译、不落 manifest** → 已解决。`worker.py` 的两条
   上下文来源（gateway 组合块 / 本地依赖结果）都经 `compile_context()` 并写
   `context_manifest`；`_render_gateway_context_block()` 这个旧的渲染死代码已删除，
   `test_context_block.py` 改为断言 `from_gateway_context_block()` +
   `render_legacy(legacy_style="gateway")`（D2）。
2. **工作区补丁无法结构化采集** → 已解决。`WorkspaceManager.head_sha()` /
   `capture_diff()` 只读采集（merge-base 基线、renamed 保留目标路径、未跟踪文件
   合成 add-patch、`max_files`/`max_diff_chars` 截断并置 `truncated`），
   `test_workspace_patch_capture.py` 4 passed。
3. **`attempt_chain.py` / `evidence.py` / `gate.py` 尚在他人手上** → 已解决。
   三个模块已落盘，符号与冻结契约一致；`test_attempt_chain.py`、
   `test_evidence_ledger.py`、`test_epistemic_gate.py` 进仓。

复核时的实际测试状态：本波次 **17 个知识层聚焦测试文件合计 259 passed**
（22 个相关知识层 / worker / context 文件合计 374 passed；2026-11-15
实测；`test_knowledge_model.py`、`test_rollout.py`、
`test_attempt_chain.py`、`test_evidence_ledger.py`、`test_epistemic_gate.py`、
`test_knowledge_loop.py`、`test_recovery_view.py`、
`test_workspace_patch_capture.py`、`test_worker_attempt_evidence.py`、
`test_worker_delivery_verdict.py`、`test_failure_signatures.py`、
`test_knowledge_claims.py`、`test_verdicts.py`、`test_knowledge_retrieval.py`、
`test_knowledge_metrics.py`、`test_knowledge_dashboard.py`、
`test_merge_arbiter_knowledge.py`、
`test_knowledge_survey.py`、`test_knowledge_deepen.py`、
`test_knowledge_pages.py`）；
其中 `test_knowledge_loop.py` 是证据→声明→校验→晋升的安全核心端到端验收。

仍存在的缺口（**不是**文档错误，是尚未完成的部分，见
[map.md](map.md) 与 [spec-p2.md](spec-p2.md)/[spec-p3.md](spec-p3.md)）：

- **claim 生产者与晋升调用方已接线**（不再缺）：`knowledge/claims.py` 生产失败/
  交付 claim，worker 在 stage ≥ `graph` 时校验 PASS 后晋升 GRAPH，
  `merge_arbiter.py::_record_integration` 在 stage `canonical` 用集成收据晋升
  REPOSITORY；`TEST_VERDICT` / `ORACLE_VERDICT` / `REVIEW_VERDICT` 已由
  `knowledge/verdicts.py` 采集，`AttemptRound.test_verdict` 用 `round_verdict()`
  标签（无裁决时仍是 `"unknown"`）。
- **P5 读取面已落盘**：`knowledge/retrieval.py` + worker
  `_knowledge_context_items` 在 stage ≥ `graph` 时把本图内已晋升 claim 注入
  `Tier.EVIDENCE`（`UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS` 默认 500）。
- **P4 知识页已落盘，自动重建已接**：`knowledge/survey.py`（AST survey）、
  `knowledge/deepen.py`（有界只读 git deepening）与 `knowledge/pages.py`
  （`knowledge_pages` 命名空间、生命周期 active/superseded/retracted、
  `rebuild()` 幂等、locked 拒绝自动改写）均已落盘并有聚焦测试；T16 读面
  （`knowledge/dashboard.py`）按生命周期聚合页面计数，T18 在 arbiter 的
  canonical 晋升后以 claim + 集成 binding 幂等重建仓库页（投影失败只计
  `pages/errors`，不影响合并）；页面仍未进入 prompt——投影接入检索仍属 Frontier。
- **端到端 A-B 指标已跑通首轮真实小样本，但尚不可判读**：
  `knowledge/metrics.py`（T15）与 CLI 已就绪；2026-11-15 用真实 worker +
  Ollama 跑了 3 个任务 × 2 arm × 1 attempt，usage 上报、配对指标与
  `--signatures` 直方图都工作，但该轮 `inconclusive`/`tokens_worse`、签名均为空，
  且 stage C 的恢复视图未被触发（attempt 0 全成功）。随后用失败注入单独验证了
  stage C：两臂各 3 次失败 attempt，C 的 `subtask_retry` 事件带回递增的 recovery
  摘要（1→2 轮、95→184 token），B 为 `None`；两条直方图都只剩一条「模型连接
  错误」签名，正好暴露归一化偏粗。可判读数据仍需成功/失败混合、失败 attempt
  两臂配对与两轮同向（见
  [runbook-ab-evaluation.md](runbook-ab-evaluation.md) §7–§8）；失败签名粒度也
  仍需在这类数据里校准。
- **门裁决可观测**：`claims.record_gate_decision` 把 allowed/denied 与理由码写成
  `VALIDATION` 证据，`knowledge/dashboard.py`（T16）聚合成理由直方图；HTTP 读面
  `GET /dashboard/api/knowledge` 已接并有 4 个路由测试，React 面板
  （`dashboard/src/lib/knowledge.ts` + `components/panels/KnowledgeOverviewPanel.tsx`）
  在 TaskDetail 内消费同一 schema（7 个 node 测试）。

## 开放问题（未决）

- 恢复视图的失败签名粒度（同义错误是否算同一签名）——`signatures.py` 已做
  去 ANSI / traceback 取末尾错误行 / 路径、UUID、hex、数字占位 / 空白折叠
  （≤200 字符），`metrics.py --signatures` 已能观测分布；粗细度仍需在 A-B
  数据里校准（判读步骤见 [runbook-ab-evaluation.md](runbook-ab-evaluation.md)）。
- 非图任务（`graph_id` 为空）如何进入 GRAPH 可见性——P5 再定；MVP 只做 attempt-local。
- 知识页的消费面与版本深度策略——`pages.py` 已落盘（`knowledge_pages`），
  `deepen.py` 只做有界只读历史观测；自动 rebuild 已接在 arbiter canonical
  晋升后（T18）；页面是否进 prompt / 同步向量库 / 占 Stage 门已由
  [D7](issues/D7-page-projection-boundary.md) 定案：均不（只读投影，消费面是
  读面与将来的 planner 视图）。
- planner 侧 1500–1800 token 视图已可调（T17，`UC_PLANNER_VIEW_BUDGET_BYTES`，
  Rust `ContextBlock` 预算，默认 8192、clamp 512–8192）；A-B 只需改 env，
  不需要第二套常量。
- Dashboard 前端面板已接：聚合模块、CLI、`GET /dashboard/api/knowledge`（T16）与
  TaskDetail 内 React 面板（`KnowledgeOverviewPanel.tsx`）共用同一 counts-only schema；
  未决的只剩决策级 A-B 数据与签名校准，不再是接口缺口。

## 结论

按 P0 → P1 → P2 → P3 推进，每阶段都有独立验收门槛（见 [spec-p0.md](spec-p0.md)
六阶段表）；P4/P5 在 MVP 闭环指标达标前保持 Frontier。默认关闭、可回滚、可归因，
是本波次与 V8 波次共享的纪律。
