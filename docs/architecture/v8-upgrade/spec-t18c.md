# Spec T18c — Durable replan accounting

Blocked by: D13 (decided). Does not implement true replan execute.

## Goal

Make `max_replans` / `replans_remaining` authoritative on the gateway by persisting `replans_used` on `WorkCase` behind an explicit flag.

## What to build

1. `WorkCase.replans_used: u32` (default 0) + proto / store round-trip
2. Flag `UC_REPLAN_ACCOUNTING_ENFORCE` (default off; truthy gate)
3. Flag on + stall with `case_id`: load case; feed stored `replans_used` into `WatchdogSignal` (ignore client shadow or audit divergence); after a `replan` verdict, CAS-increment and persist
4. Stall / related views return updated counters
5. Docs: D13, map, ops flag, trust-eval ticket 18 note; mark T18b merged (#875)
6. Tests: flag off unchanged; flag on increments on replan; exhaustion → escalate; missing case_id skip

## Acceptance criteria

- [x] Flag off: client-supplied `replans_used` path unchanged (legacy)
- [x] Flag on: stored case counter drives staircase; replan verdict increments once
- [x] Exhausted budget escalates per existing WatchdogConfig (no silent extra replan)
- [x] No digest / DAG / TaskStore / admit_execution mutation
- [x] Tests + census pins

## Out of scope

- Digest bump / re-decompose / admit_execution
- Dashboard-only UI
- Changing T18b checklist semantics beyond optional read-only remaining display
