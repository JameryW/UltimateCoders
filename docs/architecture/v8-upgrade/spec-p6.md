# Spec P6 — MetaInfer kill/control receipts (EffectLedger beyond create)

Blocked by: D16 (decided). Builds on the create-path receipts landed by P1.

## Goal

Close the remaining external side effect inside the MetaInfer boundary that has the
ledger's failure shape: the kill/control POST. Today a lost control response leaves
"termination unconfirmed" with no durable receipt. Add `kind="metainfer-kill"` so
watchdog / `preserve_effects` can see whether termination was confirmed, and so a
retry cannot silently repeat the POST.

## What to build

1. **Flag** `UC_EFFECT_LEDGER_METAINFER_CONTROL` (default off / unset = off).
2. **Identity** `stable_effect_id("metainfer-kill", backend_id, operation_id, remote_id)`
   — the logical operation is "kill this remote job", not the attempt.
3. **Termination path** (`MetaInferAdapter._execute` except branch), when the flag is
   on, `self.state` exists and `operation_id` is set:
   - `prepare(kind="metainfer-kill", request={backend_id, operation_id, remote_id})`;
     `EffectBusyError` → `RemoteStateUncertainError` (attach through the owner).
   - `CONFIRMED` → skip the POST, still run the quiescence proof and persist `stopped`.
   - `UNKNOWN` / `RECONCILING` → `RemoteStateUncertainError`; never POST again.
   - otherwise `mark_in_flight`, then POST.
   - `ok is True` → `confirm(result={"remote_id": ...})`.
   - a response with `ok != true` → `fail(error="service did not confirm kill")`
     (definite non-execution) and raise `MetaInferError("Service did not confirm kill")`.
   - transport failure / timeout / cancellation → `mark_unknown` before re-raising.
4. **Docs**: V8 map (P6 + D16), agent-os §5 row, assessment note, and the flag note
   in `docs/inference-infra.md`.

## Acceptance criteria

- [ ] Flag off: zero `tool_effects` rows and the termination path is byte-identical.
- [ ] Flag on + kill accepted → `CONFIRMED`, `kind=metainfer-kill`, `result.remote_id`.
- [ ] Flag on + lost control response → `UNKNOWN` (not `FAILED`).
- [ ] Flag on + service answers `ok != true` → `FAILED`.
- [ ] Flag on + prior `CONFIRMED` receipt → no second control POST.
- [ ] Flag on + prior `UNKNOWN` receipt → retry refused with no control POST.
- [ ] Existing create-receipt and MetaInfer reliability tests stay green.

## Out of scope

- MCP `write_file` receipts (evidence surface; own decision)
- Replacing `remote_jobs` with EffectLedger
- Any create-path change
- Wiring kill receipts into `preserve_effects` consumption
