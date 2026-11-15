# Spec P3 — Recovery View 与 Integration Receipt：把证据放回重试 prompt

Blocked by: D4（晋升条件）、D6（预算）。Builds on P1 的补丁链与 P2 的
证据/门；本阶段是**第一个改变 prompt 的 stage**（C `RECOVERY`）。

## Goal

重试不再只拿一行自由文本错误：第 2 次及以后的 attempt，把同一 subtask 链上
「上一轮改了什么、为什么失败、校验结论」编译成一个**有界、事实与假设分离**的
恢复视图，进入与首次尝试同一个预算、同一份 manifest。另把「合并/推送收据」记成
证据，供 `GRAPH → REPOSITORY` 晋升与审计使用。

## What to build

1. **纯函数视图（`python/ultimate_coders/knowledge/recovery.py`，已落盘）**：
   - 常量：`RECOVERY_SOURCE = "recovery-ledger"`、
     `RECOVERY_TITLE = "## Prior attempt evidence"`、
     `MAX_PATHS_PER_ROUND = 6`、`MAX_ROUND_DETAIL = 240`、
     `DEFAULT_MAX_ROUNDS = 5`、`MAX_HYPOTHESES = 3`。
   - `RecoveryView(attempt, rounds=(), evidence=(), hypotheses=(),
     facts_text="", hypotheses_text="", summary={})`：`.text`（事实段 + 空行 +
     假设段）、`.is_empty()`、`.tokens()`、`.to_dict()` / `.from_dict()`。
   - `build_recovery_view(*, attempt, rounds=None, evidence=None, hypotheses=None,
     budget_tokens=None, max_rounds=None)`：纯函数、不抛异常。预算默认
     `rollout.recovery_budget_tokens()`（`UC_RECOVERY_BUDGET_TOKENS`，默认 600，
     clamp 128–2000），字符预算 = `budget * 4`；取最近 `max_rounds`（默认 5）
     轮，新轮优先装，装不下丢最旧并置 `truncated=True`；假设先渲染但事实优先
     占预算（超出则 `hypotheses_text=""`）。`summary` 键：`attempt` /
     `rounds_shown` / `rounds_available` / `evidence_records` /
     `hypotheses_shown` / `tokens` / `budget_tokens` / `truncated`（log-safe）。
   - `recovery_context_items(view, *, source=RECOVERY_SOURCE) ->
     list[ContextItem]`：拆成两个 `Tier.EVIDENCE` 项——
     事实 `item_id="recovery-facts:{attempt.key()}"`、`trust_class="attempt_ledger"`、
     `verified=True`、`relevance=1.0`；假设
     `item_id="recovery-hypotheses:{attempt.key()}"`、
     `trust_class="unverified_hypothesis"`、`verified=False`、`relevance=0.6`。
     原则 A 落在这里：假设不继承事实验证状态。
2. **worker 注入（`agent/worker.py`）**：重试环 `for attempt in
   range(self.MAX_RETRIES)` 在 `attempt > 0` 时、重新路由之后调用
   `_recovery_context_block(...)`（内部先 `_load_recovery_view(...)`）；
   stage < C、首次 attempt 或无先前轮次时**原样返回首次 prompt**；有数据时用
   `compile_context(list(base_items) + recovery_items, budget_tokens=
   self._context_budget_tokens(), risk=self._last_route.risk)` 重新编译，再
   `render_legacy(legacy_style=..., gateway_truncated=...)`——统一预算 + P2 预留
   保护事实，不追加第二个预算。
3. **可观测**：重新编译后的 `manifest.summary()` 写入 `self._last_context_manifest`
   （不重复落库同一 dispatch key）；重试决策点先按 `attempt + 1` 预读视图
   （此时本 attempt 的补丁轮已落盘），`subtask_retry` 事件携带
   `data["recovery"]` = 视图 `summary`（计数/预算/是否截断，**不含内容**），
   供 A-B 成本收益对比与看板。
4. **Integration Receipt（已接线）**：`_record_delivery_evidence()` 在 workspace
   release 之后写 `EvidenceKind.MERGE_RECEIPT`
   （`payload={status, commit_sha, branch_preserved}`）与
   `EvidenceKind.PUSH_VERDICT`（`payload={push_status}`；未知时 `UNKNOWN`，
   不得当成功），全部 `visibility=attempt`；`RevisionBinding.has_integration`
   的仓库级发起方是 `merge_arbiter.py::_record_integration`（orchestrator 角色的
   合并侧），它在 stage `canonical` 时用收据校验 PASS 后晋升 REPOSITORY。
5. **开关**：`_recovery_context_block()` 在 `recovery_enabled(stage)` 为假时
   直接返回原 block（stage A/B 的重试环与升级前逐字节一致）。

## Acceptance criteria

- [x] `build_recovery_view()` 是纯函数：不读 env 以外的外部状态、不抛异常、
      同输入同输出；预算不足时丢最旧、置 `truncated`、`summary` 键齐全
      （`tests/python/test_recovery_view.py`）。
- [x] 事实与假设拆成两个 ContextItem，假设 `verified=False`；超预算时假设先让位
      且 `hypotheses_shown` 计数与实际渲染一致（`tests/python/test_recovery_view.py`）。
- [x] worker 在 `attempt > 0` 且 stage ≥ C 时才重编 context；无先前轮次 / stage A
      时 `context_block` 与首次尝试逐字相同（`tests/python/test_recovery_view.py`
      覆盖视图层；worker 端由既有 context 测试与本地 spike 保证）。
- [x] `subtask_retry` 事件带 `recovery` 摘要，且摘要中不含补丁内容/失败原文
      （`agent/worker.py::_execute_subtask` 约 1418 行）。
- [x] Integration Receipt 接线：`worker.py::_record_delivery_evidence()` 在
      workspace release 后写 `MERGE_RECEIPT` / `PUSH_VERDICT`（同文件，约 1292
      行调用）；stage A 零写入、best-effort、`UNKNOWN` 原样保留。
- [x] **晋升调用点已实现**：`merge_arbiter.py::_record_integration` 在 stage
      `canonical`、合并被接受且有 graph scope 时，把收据转成
      `PromotionRequest.integration` 并调用 `EvidenceLedger.promote()`；
      `push_status=UNKNOWN` 必被 `push_unknown` 拒绝且不改记录
      （`tests/python/test_merge_arbiter_knowledge.py`）。
- [ ] 端到端验收（MVP 闭环）：同一任务集上 stage B → C 配对比较，重复失败签名率
      与平均 attempt 数下降、成功率非劣；**测量 harness 已就绪**
      （`knowledge/metrics.py` + CLI，见 T15），失败注入已验证 C 的恢复视图在真实
      重试链上注入（runbook §8），但**决策级 B/C 数据未做**（Frontier），
      数据出来前不得宣称改善；具体阈值见 [spec-p0.md](spec-p0.md) §验收指标。

## Out of scope

- 图共享读取面已随 P5 落盘（`knowledge/retrieval.py`，stage D）；图谱化/多跳视图仍属 Frontier。
- 版本化知识页（P4）已落盘（`knowledge/pages.py`），但消费面属 Frontier；
  页面是读模型投影，不参与 Stage A–E 放量门。
- 在视图里做摘要/压缩等表达优化：本阶段只搬事实，先证明「有事实可看」有效。
- 把恢复视图写回长期记忆或知识页——它永远是 attempt-local 投影。
