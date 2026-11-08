# 37 — Reconcile leases (T37)

**What to build:** Give `RECONCILING` the same crash-detectable soft lease as
`IN_FLIGHT`. `begin_reconcile(..., lease_seconds=120)` stamps `owner` +
`lease_until` + `reconcile_attempts`; a live lease raises `EffectBusyError`, an
expired (or legacy, lease-less) one is taken over by the new owner; `resolve_reconcile`
clears the lease, and `reconciling()` lists the state so ops can see who holds what.

**Blocked by:** D7 / spec-p1 (the ledger's documented "soft lease for crash detection")

**Status:** implemented (`python/ultimate_coders/effect_ledger.py`; tests in
`tests/python/test_effect_ledger.py`)

- [x] `begin_reconcile` takes a lease and refuses a live one
- [x] An expired lease is taken over in place (`reconcile_attempts` increments)
- [x] A legacy record with no `lease_until` is takeover-able
- [x] `resolve_reconcile` clears owner/lease on both branches
- [x] `reconciling()` lists `RECONCILING` records
