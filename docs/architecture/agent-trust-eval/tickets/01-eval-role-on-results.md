# 01: eval_role on results

**What to build:** Evaluation products used for accept and reporting (`BenchmarkResult`, `OracleVerdict`, and any DTO that carries uplift) expose a required `eval_role` in `{search, select, report}`. Select and report are distinct identities: a select-tagged result must not satisfy report-only consumers, and search/select scores must never be written into fields reserved for report. UC Oracle accept may consume select; no behaviour yet changes public uplift sources (that is ticket 02).

**Blocked by:** None (can start immediately).

**Status:** implemented (branch `cursor/t1-eval-role-7c92`; flag `UC_EVAL_ROLE_ENFORCE` default off)

- [x] `BenchmarkResult` and `OracleVerdict` always carry `eval_role`
- [x] Select-tagged values are rejected by APIs/helpers that require report identity
- [x] Search-tagged values cannot be labeled or stored as report
- [x] Existing accept path can still consume select-role evidence without requiring report
- [x] Tests cover role identity rules at the Oracle / result seam (no reliance on file layout)
- [x] New enforcement remains behind a feature flag that defaults off
