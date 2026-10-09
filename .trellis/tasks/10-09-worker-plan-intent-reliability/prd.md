# Worker plan validation and edit intent reliability

## Goal and authorization

Finish the uncommitted changes found in the 2026-10-09 merge audit. The user
asked to continue unfinished tasks after being shown those five files.
The originating requirements are items 4 and 8 of the archived
06-23-optimize-scheduling-and-realtime-feedback task: validate decomposition,
retry one invalid plan, and declare/release edit intents around execution.

Baseline: `528e073b661c6e69b79f5a8c45f6ddedb5e76315`.
Branch: `codex/worker-plan-intent-reliability`.

## Requirements and acceptance

1. Accept only complete, nonempty, acyclic plans. Reject missing/non-string
   descriptions, invalid optional fields, non-integer/out-of-range/self
   dependencies, and all-identical multi-item descriptions. Legacy integer strings
   remain compatible. Validation handles deep valid dependency chains.
2. Malformed JSON, wrong output shape, empty output and invalid plans receive
   one corrective LLM retry carrying the rejection reason. Two invalid outputs
   use the existing newline fallback. Transport failure keeps immediate
   fallback; explicit inference tasks retain their single-unit routing.
3. Both local and JetStream worker execution declare file intents before the
   sandbox runs and release them on success, exception, cancellation or a
   failure partway through declaration. A release removes only the declaration
   owned by that execution, including concurrent tasks on the same worker.
   Empty path entries disappear; legacy worker-wide release stays compatible.
4. Relevant regression tests, the full Python suite, lint and repository guards
   pass. Sync executable contracts, independently review, commit, create a PR
   and merge after CI succeeds, under the user's prior delivery authorization.

## Scope boundary

No changes to the NATS wire format, distributed locking, scheduling policy,
MetaInfer safety contract or inference acceptance thresholds. The separate
metainfer-architecture task remains open for R8: on 2026-10-09 the user confirmed
that no real MetaInfer service is deployed. CPU tests cannot close that gate.
