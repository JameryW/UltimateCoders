# 36 — Operator reconciliation settles the MetaInfer receipts (T36)

**What to build:** When the operator path (`inference/reconcile.py`) proves remote state
and moves `remote_jobs`, settle the matching `metainfer-create` / `metainfer-kill`
`tool_effects` receipts too. Without it the receipt the operator just made obsolete stays
`IN_FLIGHT`/`UNKNOWN` forever: `EffectLedger.unknown()` keeps listing it, and a later kill
retry is refused as "outcome unknown" even though quiescence was proven.

**Blocked by:** D16 (same decision surface; this closes its reconciliation edge)

**Status:** implemented (`python/ultimate_coders/inference/adapter.py`,
`python/ultimate_coders/inference/reconcile.py`; tests in
`tests/python/test_metainfer_effect_settlement.py`)

- [x] `settle_metainfer_effects_from_operator_evidence(...)` confirms open receipts from proof
- [x] `attach` settles the create receipt, `confirm_stop` settles the kill receipt
- [x] `FAILED` receipts are left alone (evidence never fabricates a success)
- [x] Missing receipts are a no-op; the operator result reports `settled_effects`
