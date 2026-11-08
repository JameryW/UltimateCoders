# Spec P8 — Confirmed effects for the watchdog (preserve_effects sourcing)

Blocked by: D18 (decided). Builds on the receipt ledger from P1 / P6 / P7.

## Goal

Make the documented "a replan must not repeat confirmed side effects" path real: the
worker supplies the confirmed effect ids of the stalled attempt to the gateway
watchdog, so `Replan.preserve_effects` and the proposal's `effect:<id>` evidence carry
actual receipts instead of an always-empty list.

## What to build

1. **Ledger query** in `effect_ledger.py`:
   `EffectLedger.confirmed_effect_ids(*, branches=(), operation_prefix=None) -> list[str]`
   - only `CONFIRMED` records;
   - matches `request.branch` in `branches` **or** `request.operation_id` starts with
     `operation_prefix`;
   - sorted, de-duplicated, empty when no selector is given.
2. **Orchestrator provider**: optional `confirmed_effects_provider` callable; in the
   governed stall path, resolve it best-effort and pass the ids to `consult_watchdog`.
   Absent provider ⇒ empty tuple (previous behaviour).
3. **Worker side**: `Worker.confirmed_effect_ids_for(task, subtask, result)` queries the
   ledger with both subtask-branch forms and the domain result's `experiment_id` prefix.
4. **NATS wiring**: after constructing the `Worker`, set the orchestrator's provider.
5. **Docs**: V8 map (P8 + D18), agent-os §5 row, assessment note, and the PR06
   cross-reference that previously described an inert path.

## Acceptance criteria

- [ ] Confirmed `push-branch` records for the subtask's branch are returned; other
      branches are not.
- [ ] Confirmed MetaInfer records under the attempt's experiment prefix are returned.
- [ ] `UNKNOWN` / `FAILED` / `PREPARED` records are never returned.
- [ ] No selectors ⇒ empty list; result is sorted and de-duplicated.
- [ ] A governed stall payload carries the ids as `confirmed_effect_ids`.
- [ ] Without a provider the payload is unchanged (`[]`), and a provider exception
      does not block the stall path.
- [ ] Existing watchdog / proactive suites stay green.

## Out of scope

- Any digest / DAG / `admit_execution` change
- New effect kinds or an owner column on ledger records
- Gateway-side `preserve_effects` semantics
