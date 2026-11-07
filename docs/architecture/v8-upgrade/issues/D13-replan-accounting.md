# D13 — Durable replan accounting (T18c)

Status: decided
Decided: 2026-11-07

## Question

After T18a (proposal) and T18b (approve → checklist), what is the next safe step toward true replan execute without digest/DAG mutation?

## Decision

**T18c — durable `replans_used` on WorkCase.** Flag `UC_REPLAN_ACCOUNTING_ENFORCE` (default off). When on, the gateway owns the replan counter for a case: stall reports use the stored value (not a client-supplied shadow), and a recorded `replan` watchdog verdict CAS-increments the case counter. Return authoritative `replans_used` / `replans_remaining` on stall responses.

Do **not** bump digest, re-decompose, call `admit_execution`, or mutate TaskStore/graph. Full T18 execute remains deferred until completion_conditions product use is trusted.

## Why

Today `replans_used` is caller-supplied per stall report — the staircase cannot enforce `max_replans` honestly across restarts or hosts. Durable accounting is the quota prerequisite ticket 18 called out, and closes a trust gap before any planner execute slice.
