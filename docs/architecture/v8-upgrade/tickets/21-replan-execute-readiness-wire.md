# 21 — Replan execute readiness wire (T18d)

**What to build:** Ops runbook `docs/architecture/replan-execute-readiness-runbook.md` plus dry-run `scripts/check-replan-execute-readiness.py` with focused pytest. Align flag reporting with existing ops table names. Never set env / never mutate Compose / never call `admit_execution`.

**Blocked by:** D14

**Status:** open

- [ ] Runbook
- [ ] Dry-run script + hard blocker when runbook missing
- [ ] Tests: `tests/python/test_replan_execute_readiness.py`
