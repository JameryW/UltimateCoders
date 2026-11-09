# 41 — Prove the worker's effect scope end to end (T41)

**What to build:** The orchestrator side of `preserve_effects` was tested with an injected
fake provider, and the ledger matcher had its own tests — but the provider that actually
derives the scope (`Worker.confirmed_effect_ids_for`) had none. Cover it directly: both
`uc/subtask/<id>` branch forms, the MetaInfer `<experiment_id>:` prefix, a merge receipt
scoped by its carried branches (D22), the unscopable fallbacks, and the best-effort
failure path.

**Blocked by:** D18 (and exercises D22)

**Status:** implemented (`tests/python/test_worker_effect_scope.py`)

- [x] Coding branch and inference branch form both scope a `push-branch` receipt
- [x] `<experiment_id>:` prefix scopes a MetaInfer receipt; no experiment id ⇒ nothing
- [x] A merge receipt is scoped by the branches it carried
- [x] A branch-less merge receipt and an `UNKNOWN` receipt stay out
- [x] A lookup failure returns `[]` instead of raising
