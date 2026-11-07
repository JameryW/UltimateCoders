# Replan Proposal Spec (T18a)

> V8 P3 / D9: flag-gated `ProposalKind.replan` on watchdog `replan` verdict.
> Proposal only — never executes a new DAG or bumps digest.

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/proactive/evaluator.py`,
`python/ultimate_coders/agent/proactive_bridge.py`,
`python/ultimate_coders/agent/orchestrator.py`,
`crates/uc-types/src/governance/proactive.rs`,
`crates/uc-grpc/src/governance/proactive_service.rs`.

Trigger: changes to `UC_REPLAN_PROPOSAL_ENFORCE`, kind token `replan`, or the
stall→propose wire that passes watchdog verdicts. Do **not** mutate
`user_request`, contract digest, graph nodes, or TaskStore DAG.

---

## 2. Signatures

```python
UC_REPLAN_PROPOSAL_ENFORCE  # default off; truthy: 1/true/yes/on
ProposalKind.REPLAN = "replan"

# evaluation_context / propose_on_stall accept:
#   action, preserve_effects, contract_digest, replans_remaining
```

Rust: `ProposalKind::Replan` / `as_str() == "replan"` /
`parse_proposal_kind("replan")`.

---

## 3. Contracts

1. Flag off + `action=replan` → no REPLAN candidates (`[]` for that stall path).
2. Flag on + `action=replan` → ≤1 REPLAN; never also UNBLOCK_STALLED.
3. `action=stop` / `escalate` → existing UNBLOCK_STALLED path (unchanged eligibility).
4. Evidence includes digest / preserve_effects when supplied in context.
5. Gateway rejects unknown kind tokens; `replan` must parse lockstep.

---

## 4. Validation & Error Matrix

| Condition | Behaviour |
|---|---|
| Flag off | No `replan` kind submitted/observed |
| Flag on, action=replan | REPLAN proposal; proactive mode still gates submit |
| Flag on, action=stop | UNBLOCK_STALLED as before |
| Unknown kind on gateway | fail closed |

---

## 5. Good / Base / Bad

- **Good:** watchdog replan → observed/submitted REPLAN with digest evidence
- **Base:** flag off → zero behaviour change vs pre-P3 stop→unblock
- **Bad:** auto-executing a new DAG; rewriting user_request; emitting both REPLAN and UNBLOCK for one replan verdict

---

## 6. Tests

| Contract | Test |
|---|---|
| Flag / kind / evidence | `tests/python/test_replan_proposal.py` |
| Bridge regressions | `tests/python/test_proactive_bridge.py` |
| Rust parse round-trip | `uc-grpc` `proposal_tokens_round_trip` |

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Hard-code `action="stop"` into propose after replan verdict | Pass watchdog `action` through |
| Treat REPLAN as executable plan edit | Proposal only; DecideProposal / admit_execution remain separate |
| Flip `UC_GRAPH_SHADOW` in this change | D6 human/ops cutover stays separate |
