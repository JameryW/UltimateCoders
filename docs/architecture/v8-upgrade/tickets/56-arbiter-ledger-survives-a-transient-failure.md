# 56 — One failed `rev-parse` no longer disables receipts (T56)

**The bug.** `_push_base_branch` did this when it could not name the publish:

```python
if self._ledger is not None and effect_id is None:
    self._ledger = None          # ← the instance, not this publish
```

The intent is right — publish without a receipt rather than record under a guessed
identity — but the effect is **permanent**: the arbiter is one long-lived object in the
Orchestrator, so a single transient `rev-parse` failure (lock file, interrupted index)
turned every later publish in that process into an **unrecorded push**. The ledger's whole
job — "an ambiguous push is reconciled before you retry it" — quietly stopped applying,
and nothing said so.

**Fix.** Bind the ledger to a local for the publish (`ledger = self._ledger`) and drop
*that* when the effect cannot be named, leaving `self._ledger` intact, plus a warning so
the unrecorded publish is visible in the log. The rest of the method now uses the local,
so "no ledger this time" cannot leak into other paths by accident.

**Proof.** `tests/python/test_merge_arbiter_effects.py::test_one_failed_head_lookup_does_not_disable_receipts`
— first publish (unnameable) reports `pushed` with no `effect_id`, the arbiter still holds
its ledger, and the second publish produces a `CONFIRMED` receipt. Run against the unfixed
file it fails (1 failed / 10 passed); with the fix, 11 passed.

**Status:** implemented (`python/ultimate_coders/agent/merge_arbiter.py`,
`tests/python/test_merge_arbiter_effects.py`)

**Boundary:** the publish that cannot be named is still unrecorded by design — this task
only stops one such publish from silencing the receipts that follow it.
