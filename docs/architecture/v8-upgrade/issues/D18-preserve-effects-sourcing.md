# D18 — Confirmed effects for the watchdog (preserve_effects sourcing)

Status: decided
Decided: 2026-11-08

## Question

PR06 §6.5 requires that a replan must not repeat confirmed side effects, and the
gateway turns `WatchdogSignal.confirmed_effect_ids` into `Replan.preserve_effects`.
But the worker's `consult_watchdog` call never supplies that field — so in production
`preserve_effects` is always empty. Where do confirmed effect ids come from?

## Decision

**Derive them from the receipt ledger the worker already owns (`tool_effects`),
scoped explicitly by identifiers a record already carries, and supply them on every
governed stall.**

1. **Source** = records with `status=CONFIRMED` — the only status that means "it
   happened". `UNKNOWN` must be reconciled first and must never be preserved; the
   watchdog module documents the same rule.
2. **Scope** (effect ids are content-addressed, so there is no owner column):
   - `push-branch` records name `request.branch`; a subtask's branch is
     `uc/subtask/<12 chars>` (inference subtasks use the same prefix with a hashed
     suffix) ⇒ exact match on either form.
   - MetaInfer records name `request.operation_id` as
     `<experiment_id>:<iteration|trace>`; the attempt's experiment id is on
     `result.domain_result["experiment_id"]` ⇒ prefix match `<experiment_id>:`.
   - Other kinds (`push-main`) carried no subtask identifier here ⇒ **not guessed**, not
     included. **Narrowed by [D22](D22-push-main-scope.md):** the merge receipt now
     records the `uc/subtask/<id>` branches it carried, so a `push-main` is scoped to
     those attempts; a receipt without a branch set still stays out.
3. **Plumbing.** The orchestrator gains an optional `confirmed_effects_provider`
   callable, injected by the worker (which owns the `RuntimeState`). Without a
   provider it passes an empty tuple — byte-identical to today.
4. **Best-effort.** A provider failure logs at debug and never blocks the stall path,
   the same discipline as "a bad verdict must not crash the loop".
5. **Ids only**, never content; sorted and de-duplicated so the payload is
   deterministic.

## Why

It makes a documented safety property real instead of inert: a stalled inference
subtask that already has a live remote job (`metainfer-create` CONFIRMED) now gets a
replan proposal that names that effect instead of creating a second job. Zero new
store, zero wire change (the field exists), zero Rust change.

## Boundary

- No digest / DAG / `admit_execution` change; no change to the gateway's
  `preserve_effects` semantics.
- No new effect kind and no owner column on records — that would invent a second
  identity.
- What cannot be scoped stays empty: fewer protections, never a pretended one.

## Implementation ticket

T28: `EffectLedger.confirmed_effect_ids()` + the orchestrator provider + NATS wiring
+ focused tests.
