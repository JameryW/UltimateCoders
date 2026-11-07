# Spec P2 — Coding Champion/Challenger (Evidence OS slice)

Blocked by: D8 (decided). Complements stall redispatch (T14); does not implement true replan (T18) or graph cutover (D6).

## Goal

After a coding subtask **succeeds**, optionally run one same-contract **challenger** agent attempt tagged `eval_role=report` for compare/observe — V8 Champion/Challenger without GPU arenas or auto-promotion.

## What to build

1. `python/ultimate_coders/agent/coding_challenger.py` — pure policy + apply helpers mirroring `stall_redispatch.py`:
   - `UC_CODING_CHALLENGER_ENFORCE` (default off)
   - `UC_CODING_CHALLENGER_AGENT` optional override
   - Episode `_uc_challenger_episode` (swaps_used / last_agent / contract_digest)
   - Meta `_uc_challenger` observability
   - `decide_coding_challenger` / `apply_coding_challenger_decision` / `maybe_apply_coding_challenger`
2. Orchestrator: on successful `handle_subtask_result`, if policy says `challenge_once`, reopen Pending (do not mark Completed yet); stamp challenger agent + `eval_role=report`.
3. Docs: map P2 tickets; agent-os / trust-eval cross-link; short note that auto-promote is out of MVP.

## Acceptance criteria

- [x] Flag off: no challenger attempts; Completed path unchanged
- [x] Flag on + success: ≤1 challenger per digest; digest / `user_request` unchanged
- [x] Challenger config carries `eval_role=report` (or equivalent stamp)
- [x] Second success after challenger already used → no further reopen (champion+challenger both done → Completed)
- [x] Challenger failure (report-role) → still Completed with champion authority; no stall redispatch on the report attempt
- [x] No alternate agent → noop (keep champion Completed)
- [x] Does not run on champion failure/stall (stall owns T14)
- [x] Tests in `tests/python/test_coding_challenger.py` (+ orchestrator wire smoke)

## Out of scope

- Auto-promote challenger over champion
- MetaInfer Oracle / GPU benchmarks as coding arena
- True replan (T18)
- Graph shadow default-on (D6)
