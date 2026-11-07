# PRD — T18b replan execute checklist (D12)

## Background

V8 MVP closed (#869–#874). Approving a `replan` proposal only flips status — no next step. Full digest-bump execute stays deferred (trust-eval ticket 18).

## Requirements

1. `UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` default off
2. On approve of `ProposalKind.replan` with flag on → Attention checklist + audit; no DAG/digest mutation
3. Docs + tests

## Out of scope

admit_execution, re-decompose, digest bump, TaskStore/graph mutation, T14 conflation.

## Done when

spec-t18b AC green; focused tests pass.
