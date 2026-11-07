# D11 — V8 MVP closure

Status: decided
Decided: 2026-11-07

## Question

After P0–P4 (#869–#873), should this wayfinder stay open for true replan execute (T18), or close the V8 upgrade wave?

## Decision

**Close the V8 MVP wave.** P0–P4 delivered the Agent + Decision + Evidence hardening checklist from the assessment. Mark the map status **complete (MVP)** with:

- True replan **execute** (trust-eval ticket 18 remainder) parked as a **deferred follow-on** outside this wave — still blocked on product-stable `UC_COMPLETION_CONDITIONS_ENFORCE` use and trusted contract-version ops (unchanged from D7/D9/T18a).
- Graph-primary **default-on** still requires explicit human/ops authorization per D6/D10 (runbook + dry-run already shipped; Compose defaults stay off).
- Learning OS remains out of scope (D3).

Do not invent a silent T18 execute slice under this map without a new decision that re-opens the wave.

## Why

Frontier after P4 is empty of unblocked implementation tickets. Keeping the map “active” invites agents to grab deferred T18 and rewrite contracts. Closure records what shipped and where follow-ons live.
