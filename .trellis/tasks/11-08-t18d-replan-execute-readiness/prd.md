# PRD — T18d replan execute readiness (D14)

## Background

T18a–c merged (#872/#875/#876). True replan execute stays deferred on trust-eval ticket 18 (completion_conditions product use + contract-version ops). Graph cutover already has a runbook + dry-run (P4); replan execute lacks the same ops evidence surface.

## Requirements

1. Runbook documenting precursor flags, lockstep, and “execute still deferred”
2. `scripts/check-replan-execute-readiness.py` dry-run (no env/Compose mutation; hard-fail if runbook missing)
3. Focused pytest + docs (map marks T18c merged #876; ticket 18 note; agent-os)

## Out of scope

Digest bump, re-decompose, `admit_execution`, new execute enablement env gate.

## Done when

spec-t18d AC green; focused tests pass.
