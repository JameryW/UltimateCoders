# 07 — 证据 / 校验 / 声明账本 (T07)

**What to build:** 三类记录 + 幂等写入 + 生命周期状态机，全部落在既有
`RuntimeState`（D1）。证据是事实、校验是判定、声明是信念，分开存、分开追。

**Blocked by:** D4（三类记录与晋升语义）、D1（权威存储）
**Spec:** [spec-p2.md](../spec-p2.md)
**Status:** implemented（`python/ultimate_coders/knowledge/evidence.py`；
`tests/python/test_evidence_ledger.py`，与其它知识层聚焦测试合计 254 passed，
2026-11-15 验证）

**Implementation**

- 命名空间：`NAMESPACE = "knowledge_evidence"`、`CLAIM_NAMESPACE = "knowledge_claims"`。
- `EvidenceRecord(evidence_id, kind, attempt, scope, visibility=attempt,
  revision, payload, summary, trust_class, verified, created_at, record_version)`：
  `evidence_id` 默认 `short_digest(kind, attempt.key(), revision, 有界 payload)`；
  `trust_class` / `verified` 默认由 `model` 派生。
- `ValidationRecord(validation_id, claim_id, result, revision, evidence_ids,
  strategy, validator, notes, created_at)`：append-only，
  `validation_id = short_digest(claim, result, revision, evidence_ids, strategy,
  validator)`（`notes` 不入 digest）。
- `ClaimRecord(claim_id, statement, scope, visibility=attempt,
  status=candidate, attempt, evidence_ids, validations, binding, confidence,
  human_locked, superseded_by, retract_reason, created_at, updated_at,
  record_version)`：`claim_id = short_digest({scope, statement.casefold()})`；
  `is_terminal()` / `latest_validation()` / `promoted_visibility()`。
- `EvidenceLedger(state, *, stage=None)`：`record_evidence`、`get_evidence`、
  `evidence_for(attempt, *, kind=None, limit=100)`、`record_claim`（只写一次，
  重放返回原记录）、`get_claim`、`validate_claim`（同参幂等；首个 PASS →
  `VALIDATED`；最多 `MAX_VALIDATIONS = 20` 条）、`promote`（CAS 内重判，
  被拒不写）、`supersede`、`retract`、`lock`、`visible`（只返回
  `VALIDATED`/`PROMOTED`，稳定排序）。
- 有界与脱敏：`MAX_PAYLOAD_CHARS = 8_000`、`MAX_PAYLOAD_DEPTH = 3`、
  `MAX_PAYLOAD_ITEMS = 50`、`MAX_PAYLOAD_STRING = 2_000`、
  `MAX_SUMMARY_CHARS = 500`、`MAX_CLAIM_STATEMENT_CHARS = 2_000`、
  `MAX_ID_CHARS = 120`、`MAX_EVIDENCE_IDS = 50`、`CAS_RETRIES = 3`；
  `_REDACTED_PAYLOAD_KEYS`（prompt / system_prompt / messages / chat_history /
  transcript / diff / patch）折叠为占位。
- 写入事务：`self.state.mutate(namespace, key, apply, audit={...})`；
  审计 action 为 `record_evidence` / `record_claim` / `validate_claim` /
  `promote_claim`。

**Acceptance gates**

- [x] 证据重放幂等：同 `(kind, attempt, revision, payload)` 返回同一 id、不新增。
- [x] 声明重放幂等：已 `PROMOTED` / `RETRACTED` 的 claim 不会被重复 statement 重置。
- [x] 校验幂等与截断：同参重放不新增；`MAX_VALIDATIONS` 只保留最新 20 条；
      首个 PASS → `VALIDATED`；后续 FAIL 不回退状态但阻止晋升。
- [x] `promote()` 被拒时 claim 逐字节不变（含 `record_version`）。
- [x] 脱敏与上限：payload 里 `diff`/`prompt` 等键被折叠；超限字符串截断。
- [x] `tests/python/test_evidence_ledger.py` 覆盖上述幂等/生命周期/上限/脱敏语义。
- [x] worker 已有采集调用点：`_record_attempt_evidence`（T09）与
      `_record_delivery_evidence`（T12）；晋升调用方见
      `worker.py::_record_delivery_claim` + `knowledge/claims.py`
      （`validate_and_promote_delivery`，stage ≥ graph 才真正晋升）
      与 `merge_arbiter.py::_record_integration`（stage canonical 晋升 REPOSITORY）。
      失败 claim 由 `promote_failure_claim` 故意留空校验，只会被门拒绝。

**Rollout:** 写入不改变执行行为；晋升判定在未到 graph 阶段时由门拒绝
（`REASON_STAGE_DISABLED`），账本照常追加。stage 未启用时 `promote()` 返回
`REASON_STAGE_DISABLED`。
**Risks:** 账本是无界增长的 append-only 存储，靠 `MAX_*` 与任务收尾的
`visible(limit)` 控制读取面；`claim` 正文一旦写入不可修改（错字只能 supersede）。
