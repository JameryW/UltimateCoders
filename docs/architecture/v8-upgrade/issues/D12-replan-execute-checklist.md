# D12 — Replan execute checklist (T18b)

Status: decided
Decided: 2026-11-07

## Question

D11 closed the V8 MVP without true replan execute. What is the first safe follow-on that makes **approving** a `ProposalKind.replan` do something observable without mutating digest / DAG?

## Decision

**T18b — approve → human execute checklist only.** When `UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` is on (default off) and an operator approves a `replan` proposal, raise a structured Attention item (NeedsHuman) carrying digest / preserve_effects / proposed steps, plus a proactive audit row. Do **not** call `admit_execution`, do **not** re-decompose, do **not** bump digest, do **not** mutate TaskStore/graph.

Full planner execute (digest bump + new DAG) remains deferred on trust-eval ticket 18 until completion_conditions product use and contract-version ops are trusted.

## Why

`proactive.proposal.decide` today only flips status to `approved` — operators get no next step. Projecting a checklist closes that gap on the governance plane and preserves D9/D11 “no silent DAG rewrite.” It is distinct from T14 same-contract agent redispatch.
