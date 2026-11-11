# 58 — Operator settlement resolves the backend identity like the adapter does (T58)

**The bug.** The receipt identity is `metainfer-<kind>(backend_id, operation_id, …)`, and
the adapter that *creates* receipts resolves the backend as
`backend_id arg → UC_METAINFER_BACKEND_ID → sha256(url)`. The operator settle path in
`inference.reconcile` resolved it as `job record → sha256(url)` — **dropping the env
step**.

So for a worker configured with `UC_METAINFER_BACKEND_ID` and a job record that predates
the `backend_id` field, the operator's `confirm_stop` moved `remote_jobs` correctly and then
settled **nothing**: `settled_effects: []`, the receipt stayed `UNKNOWN`, `EffectLedger
.unknown()` kept listing it, and a later kill retry was refused as "outcome unknown" —
with no warning to say why.

**Fix.** Use the same resolution order in both places, with a comment saying it must match.

**Proof** (`tests/python/test_metainfer_effect_settlement.py`):

- `test_a_legacy_job_without_a_backend_id_uses_the_configured_identity` — job record
  without `backend_id`, receipt under `metainfer:env`, `UC_METAINFER_BACKEND_ID` set: the
  kill receipt settles. Run against the unfixed file it fails (1 failed / 7 passed).
- `test_settling_one_backend_leaves_another_backends_receipt_alone` — the backend id is
  part of the identity: settling backend A leaves backend B's receipt `UNKNOWN`.
- `test_a_confirmed_receipt_is_not_reported_twice` — a settled receipt is not a new
  settlement.

**Status:** implemented (`python/ultimate_coders/inference/reconcile.py`,
`tests/python/test_metainfer_effect_settlement.py`)

**Boundary:** only the identity resolution changed; what counts as evidence (the remote
identity proof, `confirm_quiescence`) and the CAS on `remote_jobs` are untouched. The
multi-backend tests are new coverage for an invariant that was already correct.
