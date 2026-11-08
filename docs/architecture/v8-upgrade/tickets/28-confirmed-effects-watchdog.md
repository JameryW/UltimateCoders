# 28 — Confirmed effects for the watchdog (T28)

**What to build:** `EffectLedger.confirmed_effect_ids()` scoped by branch / operation
prefix, an optional `confirmed_effects_provider` on the orchestrator's governed stall
path, `Worker.confirmed_effect_ids_for()` as the provider, and the NATS wiring — per
[spec-p8.md](../spec-p8.md). No digest/DAG change, no new effect kind, no owner column.

**Blocked by:** D18

**Status:** implemented (`python/ultimate_coders/effect_ledger.py`,
`python/ultimate_coders/agent/orchestrator.py`,
`python/ultimate_coders/agent/worker.py`, `python/ultimate_coders/nats_worker.py`;
focused cases in `tests/python/test_effect_ledger.py` and
`tests/python/test_watchdog_bridge.py`)

- [x] `confirmed_effect_ids(branches=…, operation_prefix=…)` returns CONFIRMED ids only
- [x] Orchestrator provider is optional and best-effort
- [x] Governed stall payload carries `confirmed_effect_ids`
- [x] No provider ⇒ `[]`, provider failure does not block the stall path
- [x] Tests: ledger scoping + orchestrator payload
