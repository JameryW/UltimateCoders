# Spec P3 — Replan proposal only (T18a)

Blocked by: D9 (decided). Does not implement true replan execute (T18) or graph cutover (D6).

## Goal

When the gateway watchdog returns `replan`, mint at most one structured **replan proposal** (observe/submit per proactive mode) that preserves EffectLedger ids and the current contract digest — without executing a new DAG.

## What to build

1. `ProposalKind.REPLAN = "replan"` in Python evaluator + Rust `ProposalKind::Replan` + `parse_proposal_kind`
2. Evaluator: when `progress.action == "replan"` and `UC_REPLAN_PROPOSAL_ENFORCE` on → emit REPLAN (not UNBLOCK_STALLED); include evidence for digest / preserve_effects / streak
3. Orchestrator `_propose_on_stall`: pass `action` from stall_signal `verdict` when present (`replan`/`escalate`/`stop`); attach `preserve_effects`, `contract_digest`, `replans_remaining` into evaluation context when available from the last watchdog action (thread via stall_signal or method args)
4. Docs: map P3, split note on ticket 18, agent-os gap

## Acceptance criteria

- [x] Flag off: no `replan` kind proposals; existing unblock_stalled path unchanged for `action=stop`
- [x] Flag on + `action=replan`: ≤1 REPLAN proposal; no UNBLOCK_STALLED for that evaluation
- [x] Payload carries current contract_digest when known; preserve_effects list when provided
- [x] No DAG / user_request / digest mutation helpers invoked
- [x] Gateway accepts kind token `replan` (Rust parse + Python enum lockstep)
- [x] Tests: `tests/python/test_replan_proposal.py` (+ proactive_bridge / evaluator coverage)

## Out of scope

- Auto-approve / execute replan into new DAG
- Digest / CONTRACT_VERSION bump
- Graph shadow default-on
- Changing progress.py
