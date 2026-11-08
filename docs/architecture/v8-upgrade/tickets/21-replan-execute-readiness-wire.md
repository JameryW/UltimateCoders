# 21 — Replan execute readiness wire (T18d)

**What to build:** Ops runbook `docs/architecture/replan-execute-readiness-runbook.md` plus dry-run `scripts/check-replan-execute-readiness.py` with focused pytest. Align flag reporting with existing ops table names. Never set env / never mutate Compose / never call `admit_execution`.

**Blocked by:** D14

**Status:** implemented (branch `cursor/t18d-replan-execute-readiness-e6a9`; runbook + dry-run + `tests/python/test_replan_execute_readiness.py`; truthy tokens match UC enforce flags; hard-fail when runbook missing; never sets env / never mutates Compose)

- [x] Runbook
- [x] Dry-run script + hard blocker when runbook missing
- [x] Tests: `tests/python/test_replan_execute_readiness.py`
