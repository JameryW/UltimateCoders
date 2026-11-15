# 08 — 认知门与固定拒绝词表 (T08)

**What to build:** 一个纯判决函数集：哪些 claim 允许被读、哪些允许被晋升；拒绝
必须给出**机器可读、稳定**的理由码，作为「陈旧误注入 = 0」「未授权提升 = 0」
两个验收指标的聚合键。

**Blocked by:** D4（读/晋升规则与词表）
**Spec:** [spec-p2.md](../spec-p2.md)
**Status:** implemented（`python/ultimate_coders/knowledge/gate.py`；
`tests/python/test_epistemic_gate.py` 覆盖 15 个理由码与读/晋升规则）

**Implementation**

- `GateDecision(allowed, visibility, reasons, ...)`：`.denied` / `.to_dict()` /
  `.summary()`；`reasons` 为 `REASON_*` 元组。
- `PromotionRequest(claim_visibility=attempt, target_visibility=repository,
  scope=KnowledgeScope(), status=candidate, attempt_succeeded=False,
  attempt_stale=False, validation_result=None, validation_revision="",
  target_revision="", integration=None, push_status="", human_locked=False)`。
- `EpistemicGate.can_read(*, record_visibility, record_scope,
  reader_visibility=None, reader_scope=None, binding=None, reader_revision="",
  same_chain=False) -> GateDecision`：
  - `attempt`：仅 `same_chain=True`，否则 `attempt_chain_required`；
  - `graph`：记录与读者的 graph_id、tenant、repo、branch 必须同时匹配，
    否则 `graph_not_shared`；
  - `repository`：`scope.allows_read()` + `binding.applies_to(reader_revision)`，
    否则 `cross_scope_denied` / `revision_mismatch`。
- `EpistemicGate.evaluate_promotion(request) -> GateDecision`：
  `target_not_higher`、`claim_terminal`、`attempt_not_successful`、
  `attempt_stale`、`validation_missing` / `validation_failed` /
  `validation_inconclusive`、`revision_mismatch`、`graph_scope_missing`、
  `integration_missing`、`push_unknown`（`push_status.upper() == "UNKNOWN"` 视为未知）；
  stage 未启用 → `stage_disabled`。
- 词表常量共 15 个（含 `REASON_CROSS_SCOPE_DENIED`、`REASON_ATTEMPT_CHAIN_REQUIRED`），
  `ALL_REASON_CODES` 供 dashboard / 测试枚举。

**Acceptance gates**

- [x] 每个 `REASON_*` 至少一条触发用例；理由码不得改写（指标依赖）。
- [x] `can_read` 默认拒绝：未知/空 reader 信息不放宽；空 scope 记录字段通配。
- [x] `evaluate_promotion` 对同一请求是纯函数（不含时间/IO）。
- [x] 未知 stage → `stage_disabled`；未知枚举值在 `model.normalize_*` 抛错。
- [x] `tests/python/test_epistemic_gate.py` 覆盖每个理由码与默认拒绝语义。
- [x] `tests/python/test_knowledge_loop.py` 端到端验收门与账本的合成行为：
      未校验不可晋升、失败 attempt 不可晋升、`UNKNOWN` 推送不算成功、拒绝不改
      记录（`record_version` 不变）、晋升重放幂等（`target_not_higher`）。
- [x] `EvidenceLedger.visible()` / `promote()` 的宿主调用方已就位：
      `knowledge/claims.py`（`validate_and_promote_delivery` / `promote_failure_claim`）
      经 `worker.py::_record_delivery_claim`（stage ≥ graph 晋升 GRAPH）与
      `merge_arbiter.py::_record_integration`（stage canonical 晋升 REPOSITORY）调用；
      worker 重试视图走 `AttemptChainLedger`（attempt 链本身就是最小可见性），
      P5 图共享读取面 `knowledge/retrieval.py::retrieve_claims` 同样过门
      （stage < graph 返回空）。

**Rollout:** 门本身无开关；**强制**在 stage ≥ graph 生效（恢复视图读取 attempt
链时使用），stage shadow / recovery 只把判定写日志。
**Risks:** 词表增加会改变指标口径——新增原因码须同时更新 spec-p0 指标表与
看板聚合；`push_unknown` 依赖调用方如实上报，不能用默认值糊过去。
