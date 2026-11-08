# 38 — Fence a stale reconciler (T38)

**What to build:** `resolve_reconcile(..., owner=...)` refuses to settle a record whose
lease is still live and belongs to someone else. Both real reconcilers pass their owner
(`merge-arbiter`, `release-reconcile`), so a reconciler that was taken over can no longer
settle the effect with a verdict it derived before the takeover.

**Blocked by:** D7 / spec-p1 (follows T37's lease)

**Status:** implemented (`python/ultimate_coders/effect_ledger.py`,
`python/ultimate_coders/agent/merge_arbiter.py`,
`python/ultimate_coders/agent/workspace.py`; tests in `tests/python/test_effect_ledger.py`)

- [x] Live lease held by another owner → `EffectBusyError`, record untouched
- [x] The lease holder settles its own reconcile
- [x] An expired lease lets the new owner settle
- [x] Ownerless callers stay backward compatible
