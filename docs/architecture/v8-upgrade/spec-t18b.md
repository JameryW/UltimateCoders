# Spec T18b — Replan execute checklist

Blocked by: D12 (decided). Does not implement full true replan execute (T18 remainder).

## Goal

When an operator **approves** a `ProposalKind.replan` proposal under an explicit flag, project a human-facing execute checklist (Attention + audit) without mutating digest, DAG, or TaskStore.

## What to build

1. Flag `UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` (default off; truthy gate matching other enforce flags)
2. Post-`decide_proposal` hook: if flag on and kind is Replan and decision is approve → raise Attention (NeedsHuman) on `case_id` with structured detail (digest / effects / proposed_request / human_action steps); emit proactive audit `replan_execute_checklist`
3. Flag off / reject / non-replan kinds: behavior unchanged
4. Docs: D12 + this spec + ticket(s); ops flag table; note on trust-eval ticket 18; agent-os brief
5. Tests: Rust and/or Python covering flag off, approve→attention, reject→no attention, missing case_id warn/skip

## Acceptance criteria

- [x] Flag off: approve replan → no new attention / no checklist audit
- [x] Flag on + approve replan: ≤1 attention item; DAG/digest untouched
- [x] Flag on + reject / other kinds: no checklist side-effect
- [x] Missing `case_id`: skip projection with warn (escalate_to_attention precedent)
- [x] No `admit_execution` / `mark_executed` / orchestrator decompose / TaskStore mutation
- [x] Tests green; census pins if needed

## Out of scope

- Digest bump / `ContractRegistry` rewrite
- Mid-task `_decompose_task` / new DAG
- `admit_execution` → auto submit
- Conflating with `UC_STALL_REDISPATCH_ENFORCE` (T14)
- Graph shadow default-on
