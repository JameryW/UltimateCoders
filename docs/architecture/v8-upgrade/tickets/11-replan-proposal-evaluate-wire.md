# 11 — Evaluator + orchestrator replan proposal wire

Status: implemented
Blocked by: 10

## What to build

- `UC_REPLAN_PROPOSAL_ENFORCE` (default off)
- Evaluator emits `ProposalKind.REPLAN` when `progress.action == "replan"` and flag on; suppress UNBLOCK_STALLED for that case
- Orchestrator passes watchdog verdict as `action` into `propose_on_stall` and attaches digest / preserve_effects into context when present on the stall_signal / WatchdogAction

## Acceptance criteria

Per spec-p3.md; tests in `test_replan_proposal.py`
