# 24 — MetaInfer kill/control EffectLedger receipt (T24)

**What to build:** Wrap `MetaInferAdapter._execute`'s termination path in the
EffectLedger under `UC_EFFECT_LEDGER_METAINFER_CONTROL` (default off), per
[spec-p6.md](../spec-p6.md). New kind `metainfer-kill`; `CONFIRMED` skips the repeat
POST, `UNKNOWN`/`RECONCILING` refuses the retry, `ok != true` becomes `FAILED`, a
transport failure becomes `UNKNOWN`. Never touch the create path, `remote_jobs`
semantics, or any digest.

**Blocked by:** D16

**Status:** implemented (`python/ultimate_coders/inference/adapter.py`; focused
cases in `tests/python/test_metainfer_effects.py`; flag off leaves zero
`tool_effects` rows and the termination path byte-identical)

- [x] Flag `UC_EFFECT_LEDGER_METAINFER_CONTROL` (default off)
- [x] `stable_effect_id("metainfer-kill", backend_id, operation_id, remote_id)`
- [x] `CONFIRMED` → no second control POST; `UNKNOWN`/`RECONCILING` → retry refused
- [x] `ok != true` → `FAILED`; lost response → `UNKNOWN`
- [x] Tests: `tests/python/test_metainfer_effects.py`
