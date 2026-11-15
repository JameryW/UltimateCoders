# Spec P2 — Evidence Ledger 与 Epistemic Gate：可追的证据 + 可拒的门

Blocked by: D4（已决定）、D1（权威存储已决定）。Builds on P1 的
`AttemptRound` 身份与 P0 的 `Visibility` / `RevisionBinding` 词汇。

## Goal

把「系统相信什么」拆成三层：**事实（evidence）→ 判定（validation）→ 信念
（claim）**，并让所有跨 attempt 的读取（重试恢复视图、未来的图共享）都经过一个
可测试、fail-closed 的门。本阶段**默认仍只记录、不改变行为**（stage B）：门照常
判定并把拒绝理由写进日志/审计，是否强拒留到 stage C（P3）。

## What to build

1. **证据账本（`python/ultimate_coders/knowledge/evidence.py`）**，命名空间
   `knowledge_evidence` / `knowledge_claims`：
   - `record_evidence(*, attempt, kind, payload=None, scope=None,
     visibility=Visibility.ATTEMPT, revision=None, summary="", trust_class=None,
     verified=None, evidence_id=None) -> EvidenceRecord`：id 默认由
     `(kind, attempt.key(), revision, 有界 payload)` 的 `short_digest` 决定，
     重放幂等；`trust_class`/`verified` 默认取 `model.DEFAULT_EVIDENCE_TRUST` /
     `VERIFIED_EVIDENCE_KINDS`，调用方不得自封「已验证」。
   - `evidence_for(attempt, *, kind=None, limit=100)` / `get_evidence(id)`。
   - `record_claim(*, statement, scope, attempt=None, evidence_ids=(),
     binding=None, visibility=Visibility.ATTEMPT, confidence=0.0)`：
     `claim_id = short_digest(scope + statement.casefold())`，**只写一次**，
     重放返回原记录（状态/校验/可见性都不变）。
   - `validate_claim(claim_id, *, result, revision="", evidence_ids=(),
     strategy="", validator="", notes="")`：append-only，
     `validation_id` 由 `(claim, result, revision, evidence_ids, strategy,
     validator)` 决定；同参重放幂等；首个 `PASS` 把 claim 推到 `VALIDATED`，
     后来的 `FAIL` 不会抹掉历史，但门只看**最新**一条，因此仍会阻止晋升。
     每 claim 最多保留 `MAX_VALIDATIONS = 20` 条。
   - `promote(claim_id, *, target=Visibility.GRAPH, attempt_succeeded=False,
     attempt_stale=False, integration=None, push_status="", target_revision="",
     gate=None, human_locked=None) -> (ClaimRecord, GateDecision)`：门在
     CAS 事务内**重新判定**，被拒时返回原 claim 且不写任何字段；
     `_PromotionDenied` 只用于事务内并发退化。
   - `supersede()` / `retract()` / `lock()` / `visible(*, scope,
     reader_visibility, reader_scope=None, reader_revision="", same_chain=False,
     min_visibility=Visibility.ATTEMPT, limit=50)`：`visible()` 只返回
     `VALIDATED`/`PROMOTED`，按 可见性 rank 降序 → confidence 降序 → id 升序
     稳定排序。
2. **认知门（`python/ultimate_coders/knowledge/gate.py`）**：
   - `EpistemicGate.can_read(*, record_visibility, record_scope,
     reader_visibility=None, reader_scope=None, binding=None,
     reader_revision="", same_chain=False) -> GateDecision`：
     `ATTEMPT` 需 `same_chain=True`；`GRAPH` 需 graph_id 与 tenant/repo/branch
     同时匹配；`REPOSITORY` 需 `scope.allows_read()` + `binding.applies_to()`。
   - `EpistemicGate.evaluate_promotion(request: PromotionRequest) ->
     GateDecision`：晋升规则见 [D4](issues/D4-evidence-claim-separation.md)
     第 4 条（目标 rank 严格更高、非终态、attempt 成功且未过期、最新校验 PASS
     且修订一致、GRAPH 需 graph_id、REPOSITORY 需集成收据且推送状态已知）。
   - `GateDecision(allowed, visibility, reasons)`：`reasons` 是固定词表
     `REASON_*`（15 个），`summary()` 给日志用的紧凑 dict；**拒绝原因即指标键**。
3. **有界与脱敏**：payload 走 `_bounded_payload()`
   （`MAX_PAYLOAD_CHARS = 8_000` / `MAX_PAYLOAD_DEPTH = 3` /
   `MAX_PAYLOAD_ITEMS = 50` / 字符串截断 2 000），`_REDACTED_PAYLOAD_KEYS`
   （`prompt` / `system_prompt` / `messages` / `chat_history` / `transcript` /
   `diff` / `patch`）折叠为占位；statement ≤ 2 000，summary ≤ 500，id ≤ 120。
4. **默认只记录**：`EvidenceLedger(state, *, stage=None)` 的写入与
   `visible()` 判定在 stage < RECOVERY 时不做强拒（gate 结果只进审计/日志）；
   `promote()` 在 stage 未启用时以 `REASON_STAGE_DISABLED` 拒绝。
5. **审计**：每次落库带 `audit={"action": ...}`（`record_evidence` /
   `record_claim` / `validate_claim` / `promote_claim`），便于事后回答
   「这条 claim 是怎么升上来的」。

## Acceptance criteria

- [x] `record_evidence()` 幂等：同 `(kind, attempt, revision, payload)` 重放
      返回同一 `evidence_id`，不新增记录；不同 payload 得到不同 id。
- [x] `record_claim()` 同 scope + 同 statement（大小写/casefold 归一化）返回
      原 claim，已 `PROMOTED`/`RETRACTED` 的状态不会被重放重置。
- [x] `validate_claim()` 同参重放幂等；首个 PASS → `VALIDATED`；后续 FAIL 不
      回退状态但会阻止晋升；`MAX_VALIDATIONS` 截断只保留最新 20 条。
- [x] `promote()` 被拒时 claim 逐字节不变（含 `record_version`），并返回含
      `REASON_*` 的 `GateDecision`；允许时同一事务写入 `visibility` /
      `status=PROMOTED` / `human_locked`。
- [x] `can_read()`：ATTEMPT 跨链拒绝（`attempt_chain_required`）、GRAPH 缺
      graph_id 拒绝（`graph_not_shared`）、REPOSITORY 修订不匹配拒绝
      （`revision_mismatch`）；空 scope 字段是通配、空 reader 字段不放宽。
- [x] `evaluate_promotion()` 15 个 `REASON_*` 每个至少一条用例；未知 stage
      返回 `stage_disabled`，未知枚举值在 `normalize_*` 抛 `ValueError`。
- [x] `visible()` 只返回 `VALIDATED`/`PROMOTED`，过滤终态、最小可见性与
      修订绑定，排序稳定。
- [x] 测试文件 `tests/python/test_evidence_ledger.py`、
      `tests/python/test_epistemic_gate.py` 已进仓并通过；另有端到端安全核心验收
      `tests/python/test_knowledge_loop.py`（证据 → 声明 → 校验 → 晋升，含
      `UNKNOWN` 推送拒绝与重放幂等）。本波次 17 个知识层聚焦测试合计
      **254 passed**（24 个相关文件合计 382 passed），2026-11-15 实测。
- [x] worker 侧采集已接线：`_record_attempt_evidence()` 写
      `ATTEMPT_OUTCOME` / 结构化 `REVIEW_VERDICT` / `ORACLE_VERDICT` /
      `TEST_VERDICT` / `FAILURE_SIGNATURE`（T09、T13），
      `_record_delivery_evidence()` 写 `MERGE_RECEIPT` / `PUSH_VERDICT`（T12），
      采集侧默认保持 `visibility=attempt`。
- [x] **claim 生产者与晋升调用方已接线**：`knowledge/claims.py` 生产失败 claim
      （只 ATTEMPT，故意不校验 → 门以 `validation_missing` 拒绝晋升）与交付 claim
      （`record_delivery_claim` 先写 claim，`validate_and_promote_delivery` 在
      stage ≥ `graph` 且 merge 被接受、revision 与 `graph_id` 齐备时先校验 PASS
      再晋升 GRAPH，`attempt_succeeded` 透传 `result.success`，失败 attempt 不晋升）；
      `merge_arbiter.py::_record_integration` 在 stage `canonical` 用集成收据
      （`integration_ref` / `integration_sha`）晋升 REPOSITORY。调用点见
      `tests/python/test_knowledge_claims.py`、`test_worker_attempt_evidence.py`、
      `test_merge_arbiter_knowledge.py`。

## Out of scope

- 检索排序、按视图预算（P5）；知识页（P4）。
- 自动判断 claim 真假：只消费 oracle / test runner / reviewer / human 的结论。
- 跨租户合规策略扩展：`KnowledgeScope.allows_read()` 是唯一裁决点。
- 第二套存储：全部走 `RuntimeState`（D1）。
