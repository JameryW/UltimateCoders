# D10 — Graph cutover runbook scope (P4)

Status: decided
Decided: 2026-11-07

## Question

Given D6 (no default flip of `UC_GRAPH_SHADOW` in V8 P0–P3), what is the first shippable P4 slice for graph-primary cutover?

## Decision

**Ops runbook + dry-run readiness check only.** Deliver:

1. A human-facing cutover/rollback runbook that cites existing durable-runtime upgrade notes and the exact `UC_GRAPH_SHADOW=on` gate.
2. A dry-run validation script that reports readiness (DB URL / storage feature expectations / docs pointers) **without** enabling shadow mode or mutating Compose defaults.

Do **not** flip Compose/`UC_GRAPH_SHADOW` defaults, do **not** make graph-only authority automatic from `UC_DATABASE_URL`, and do **not** implement true replan execute (T18).

## Why

D6 already made cutover ready-for-human. The missing artifact is the ops checklist + a non-destructive readiness probe so an operator can authorize a cutover with evidence. Code for lease/fence/commit-once largely exists; shipping default-on would violate D6.
