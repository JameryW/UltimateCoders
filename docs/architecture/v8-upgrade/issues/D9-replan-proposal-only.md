# D9 — Replan proposal only (T18a)

Status: decided
Decided: 2026-11-07

## Question

Is full true replan (new decomposition / digest bump / execute DAG) ready for V8 P3, or should the first slice only mint a structured **proposal**?

## Decision

**Proposal only (T18a).** On watchdog `replan` verdict, optionally submit a `ProposalKind::Replan` (`"replan"`) carrying stall evidence, current `contract_digest`, and `preserve_effects`. Flag `UC_REPLAN_PROPOSAL_ENFORCE` default off. Never mutate `user_request`, digest, graph nodes, or TaskStore DAG. Full execute remains deferred T18.

## Why

Orchestrator already stops on `replan` and calls proactive with `action="stop"`, so the gateway staircase has nowhere to put a replan suggestion. Closing that gap is the natural precursor to true replan and complements P0–P2 without ops cutover or silent contract rewrite.
