# 27 — Workload role provenance in the experiment checkpoint (T27)

**What to build:** Finish D11 on the resume path — fail fast when a `report_benchmark`
declares a non-report role, record the workload role in checkpoint.json, and refuse a
resume whose declared role differs (a legacy checkpoint with no recorded role may only
be resumed by a `select` run).

**Blocked by:** D11 (same decision; this closes its checkpoint/fail-fast edges)

**Status:** implemented (`python/ultimate_coders/inference/workflow.py`; tests in
`tests/python/test_workload_role_resume.py`)

- [x] `report_benchmark` with a non-report spec raises up front (message names the role)
- [x] checkpoint.json records `eval_role`
- [x] Resume with a different role is refused, naming both roles
- [x] Legacy checkpoint (no `eval_role`) refuses a `report`/`search` resume, allows `select`
