# PRD — V8 P3 replan proposal only (T18a)

## Background

P0–P2 merged. P3 closes the watchdog `replan` → proactive gap with a structured proposal only (D9). Full DAG execute stays deferred.

Tickets 10–12; spec-p3.md.

## Requirements

1. `ProposalKind.replan` lockstep Rust+Python
2. Flag-gated evaluator + orchestrator wire
3. Docs/map

## Out of scope

Execute replan, digest bump, graph shadow default-on.

## Done when

spec-p3 AC green; focused pytest (+ Rust kind parse test) pass.
