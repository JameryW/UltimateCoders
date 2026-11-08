# D14 — Replan execute readiness (T18d)

Status: decided
Decided: 2026-11-08

## Question

After T18a (proposal), T18b (approve → checklist), and T18c (durable accounting), what is the next safe step toward true replan execute without digest/DAG mutation or `admit_execution`?

## Decision

**T18d — ops readiness runbook + dry-run probe only.** Deliver:

1. A human-facing readiness/rollback-minded runbook that cites T18a–c flags, completion-conditions product use, and contract-version lockstep notes, and states that true execute remains deferred on trust-eval ticket 18.
2. A dry-run validation script that reports precursor-flag state and docs presence **without** enabling any replan-execute gate, mutating Compose, bumping digest, re-decomposing, or calling `admit_execution`.

Do **not** invent or flip an execute env default, do **not** call `admit_execution`, and do **not** mutate TaskStore/graph.

## Why

Ticket 18 stays blocked on trusted completion-conditions product use and contract-version ops. The missing artifact is the same class of ops evidence P4 shipped for graph cutover (D10): a checklist plus a non-destructive probe so a human can later authorize execute with evidence. Shipping silent execute would violate D11 / D9.
