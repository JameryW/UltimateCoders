# 08 — Orchestrator wiring for coding challenger

Status: implemented
Blocked by: 07

## What to build

In `handle_subtask_result`, when `result.success` and policy returns `challenge_once`, reopen Pending (do not mark Completed). When a later result arrives and challenger episode is already used (`eval_role=report`), mark Completed and keep compare meta — including when the report-role challenger **fails** (observe-only: champion stays authoritative; do not stall-redispatch the report attempt). Ignore duplicates similar to stall redispatch.

## Acceptance criteria

- Flag off: existing success path unchanged
- Flag on: first success reopens; second attempt Completes the subtask (success or report failure)
- Worker load freed on reopen (same as stall path)
- Test covering orchestrator wire (unit or light integration), including challenger-failure → champion Completed
