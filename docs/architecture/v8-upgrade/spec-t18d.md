# Spec T18d — Replan execute readiness

Blocked by: D14 (decided). Does not implement true replan execute (T18 remainder).

## Goal

Make true replan execute **ready for an explicit human/ops authorization later**: a checklist runbook plus a dry-run readiness script that never enables execute, never bumps digest, and never calls `admit_execution`.

## What to build

1. Runbook `docs/architecture/replan-execute-readiness-runbook.md` citing T18a–c flags, `UC_COMPLETION_CONDITIONS_ENFORCE`, durable-runtime contract lockstep, and trust-eval ticket 18 deferral.
2. Dry-run script `scripts/check-replan-execute-readiness.py` that prints precursor-flag / docs checklist; exits non-zero on hard blockers (e.g. missing runbook); **must not** set env vars or rewrite Compose.
3. Focused pytest for pure helpers (`tests/python/test_replan_execute_readiness.py`).
4. Docs: D14, map (mark T18c **merged** #876), ops pointer, trust-eval ticket 18 note, agent-os brief.

## Acceptance criteria

- [ ] Runbook present and states execute remains deferred / no silent DAG rewrite
- [ ] Dry-run never mutates env or Compose; hard-fails when runbook missing
- [ ] Reports proposal / checklist / accounting / completion_conditions flag states
- [ ] Tests cover helper parsing + non-mutation
- [ ] Map / agent-os / ticket 18 note T18d readiness without satisfying full execute

## Out of scope

- Digest bump / re-decompose / `admit_execution`
- New `UC_REPLAN_EXECUTE_ENFORCE` (or similar) enablement gate
- Graph-primary default-on / Learning OS
