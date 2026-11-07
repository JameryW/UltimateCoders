# 04 — MetaInfer create EffectLedger receipts

Status: implemented
Blocked by: none (D7 decided)

## What to build

Wire `MetaInferAdapter` create POST to EffectLedger when `UC_EFFECT_LEDGER_METAINFER` is truthy, per [spec-p1.md](../spec-p1.md).

## Acceptance criteria

- Env flag default off; no ledger writes when off
- `kind=metainfer-create`; id = `stable_effect_id("metainfer-create", backend_id, operation_id, request_sha256)`
- Success → CONFIRMED; uncertain POST → UNKNOWN; CONFIRMED prepare skips second POST
- Tests in `tests/python/test_metainfer_effects.py`
