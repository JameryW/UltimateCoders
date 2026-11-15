# 01 — 冻结知识层词汇与规范化 (T01)

**What to build:** 一个 import-light 的纯类型模块，冻结后续所有阶段共享的身份与
枚举：attempt 身份、可见性层级、证据种类与默认信任类、claim 状态机、校验结果、
scope、修订绑定，以及确定性 id / 日志安全文本工具。

**Blocked by:** 无（D1 / D4 已决定存储与三类记录的语义）
**Spec:** [spec-p0.md](../spec-p0.md)
**Status:** implemented（`python/ultimate_coders/knowledge/model.py`；
`tests/python/test_knowledge_model.py`，另由 `test_attempt_chain.py` /
`test_recovery_view.py` / `test_epistemic_gate.py` 间接覆盖 `AttemptRef` 等身份）

**Implementation**

- `AttemptRef(task_id, subtask_id, dispatch_attempt=0, attempt=0, worker_id="",
  worker_epoch=0, graph_id="")`：`key()` = `task:subtask:dispatch:attempt`、
  `order()`、`is_before()`、`next_attempt()`、`to_dict/from_dict`。
- `Visibility`（`attempt` / `graph` / `repository` + `VISIBILITY_RANK`，
  `normalize_visibility` 接受 attempt-local/local、task、repo/canonical/long-term，
  未知值抛 `ValueError`）。
- `EvidenceKind`（`patch_observed` / `attempt_outcome` / `failure_signature` /
  `oracle_verdict` / `test_verdict` / `review_verdict` / `merge_receipt` /
  `push_verdict` / `hypothesis` / `validation` / `human_note`）+
  `DEFAULT_EVIDENCE_TRUST`、`VERIFIED_EVIDENCE_KINDS`、`default_trust_class()`、
  `default_verified()`。
- `ClaimStatus`（candidate/validated/promoted/superseded/retracted）、
  `ValidationResult`（pass/fail/inconclusive，布尔与空串归一化）+
  `validation_result_passed()`。
- `KnowledgeScope(tenant_id="default", repo_id="", branch="", graph_id="")` +
  `allows_read()`（空记录字段通配，空 reader 字段不放宽）；
  `RevisionBinding(repo_id, branch, revision, base_revision, integration_ref,
  integration_sha)` + `has_integration` / `applies_to()`（短 SHA 前缀 ≥7 位）。
- 工具：`canonical_json`、`digest`（SHA-256）、`short_digest(value, length=16)`、
  `safe_text(value, limit=2000)`、`utc_now()` / `utc_now_iso()`。

**Acceptance gates**

- [x] 所有 `normalize_*` 对未知取值 fail-closed（抛 `ValueError`），不静默回退。
- [x] `short_digest` 长度 clamp 4–64；同输入同输出；`safe_text` 去 NUL 且截断带标记。
- [x] `KnowledgeScope.allows_read()`：租户必须一致、空记录字段通配、空 reader 字段
      不放宽（P2 门的读规则依赖它）。
- [x] `RevisionBinding.applies_to()`：空 pin = 通配，短 SHA 前缀有歧义时不匹配。
- [x] 独立测试文件 `tests/python/test_knowledge_model.py` 覆盖 identity / 枚举归一 /
      scope 与修订绑定规则。

**Rollout:** 无开关（纯函数库，不读 env）。
**Risks:** 枚举别名只增不减会产生语义漂移——新增别名须同时改本文件与 D4 词表；
`ValueError` 依赖调用方 fail-closed（P2 账本入口统一 `_*_value()` 包装）。
