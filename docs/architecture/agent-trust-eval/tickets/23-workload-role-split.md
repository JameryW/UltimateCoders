# 23 — Workload role + report hold-out boundary (T23)

**What to build:** `UC_EVAL_ROLE_SPLIT_ENFORCE` (default off), `BenchmarkSpec.eval_role`
with runner stamping, `mint_report_result` cross-role refusal, and
`OptimizationWorkflow.run(..., report_benchmark=...)` with fail-closed selection — per
[spec-p4.md](../spec-p4.md). No dataset format, no new storage, no second Oracle.

**Blocked by:** D11

**Status:** implemented (`python/ultimate_coders/inference/models.py`,
`python/ultimate_coders/inference/benchmark.py`,
`python/ultimate_coders/inference/workflow.py`; tests in
`tests/python/test_eval_role.py` and `tests/python/test_report_uplift.py`)

- [x] `BenchmarkSpec.eval_role` (default select) and runner stamping (success + early return)
- [x] `mint_report_result` refuses cross-role minting under the flag; report → report is a no-op
- [x] `report_benchmark` used for the remeasure; missing one fails closed under the flag
- [x] Flag off is byte-identical to today
