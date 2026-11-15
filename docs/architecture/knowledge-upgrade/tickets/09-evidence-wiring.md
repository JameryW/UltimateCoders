# 09 — 证据采集接线与聚焦测试 (T09)

**What to build:** 把 P1 已经产生的事实（补丁轮次 / 尝试结局）接进
`EvidenceLedger`，让重试链的结论与「改了什么」分开存；补齐账本与门的聚焦测试。

**Blocked by:** T07（账本）、T08（门）
**Spec:** [spec-p2.md](../spec-p2.md)
**Status:** implemented（`python/ultimate_coders/agent/worker.py::
_record_attempt_evidence` / `_verification_report`；结构化裁决采集在
`knowledge/verdicts.py`，签名归一化在 `knowledge/signatures.py`，claim 生产在
`knowledge/claims.py`；`tests/python/test_worker_attempt_evidence.py` 12 tests、
`test_verdicts.py` 18、`test_knowledge_claims.py` 17、`test_failure_signatures.py` 9；
账本/门测试见 T07/T08）

**Implementation**

1. `_record_attempt_evidence(subtask, attempt, result)`：在
   `_record_attempt_round()` 之后、checkpoint/release 之前调用，`stage =
   rollout.stage_from_env()`，`capture_enabled` 为假直接返回（best-effort，
   异常只 `logger.debug`）。
   - `EvidenceKind.ATTEMPT_OUTCOME`：`payload={success, retryable,
     cleanup_pending, attempt, retry_count, changed_files}`（`changed_files` 是
     数量，不是文件内容）、`summary=result.summary`；
   - 失败时再写一条 `EvidenceKind.FAILURE_SIGNATURE`：
     `payload={"signature": normalize_failure_signature(...), "excerpt":
     (result.error or result.summary or "")[:500]}`——`signature` 是归一化后的
     身份（`knowledge/signatures.py`，≤200 字符），`excerpt` 保留原文供人看；
     失败 claim 的 statement 使用归一化签名。
   - 身份是 `self._attempt_ref(subtask, attempt)`；记录默认
     `Visibility.ATTEMPT`，**不**跨 attempt 链可见。
2. 与补丁轮次的分工：轮次记录 git 观察到的事实（改了什么），证据记录 attempt
   得出的结论（成功/可重试/失败签名）；失败的 error 文本是签名，**不是** oracle
   裁决（原则 A）。
3. 测试：`tests/python/test_worker_attempt_evidence.py` 覆盖成功/失败/stage 关闭/
   异常吞掉/身份与可见性；账本幂等与门词表分别在
   `test_evidence_ledger.py` / `test_epistemic_gate.py`。

**Acceptance gates**

- [x] 失败 attempt 至少产生 `attempt_outcome` + `failure_signature` 两条证据。
- [x] 同 attempt 重放不重复（证据 id 由 kind/attempt/revision/payload 派生）。
- [x] 证据 payload 不含 prompt / 完整 diff；`changed_files` 只是计数。
- [x] stage A（默认）下零写入、零行为变化；捕获失败不影响子任务结果。
- [x] 全部记录保持 `visibility=attempt`（测试逐条断言）。
- [x] **claim 生产者已接线**：`knowledge/claims.py` 把失败结论与交付结论变成
      claim；worker 在 `stage ≥ graph`、merge 被接受且有修订与 graph scope 时
      校验 PASS（`strategy="merge-receipt"`）并晋升交付 claim，失败 attempt 不
      晋升；`test_worker_attempt_evidence.py` / `test_knowledge_claims.py` 覆盖。
- [x] **结构化裁决采集**：`SubtaskResult.review` 与 `domain_result` 里的
      review/oracle/test 裁决写成 `REVIEW_VERDICT` / `ORACLE_VERDICT` /
      `TEST_VERDICT`，`AttemptRound.test_verdict` 使用 `round_verdict()` 标签
      （无裁决时仍是 `"unknown"`）；见 `knowledge/verdicts.py` 与
      `test_verdicts.py`。

**Rollout:** 写入随 `UC_KNOWLEDGE_STAGE`≥`shadow`；默认关。
**Risks:** `failure_signature` 归一化过粗会掩盖真差异、过细会让「重复失败」永远
为 0——已在 `knowledge/signatures.py` 做去 ANSI、traceback 取末尾错误行、
路径/UUID/hex/数字占位与空白折叠（≤200 字符，全噪声返回空串），
`repeated_failure_rate` 因此可用；粗细度仍需在 A-B 数据里校准。
