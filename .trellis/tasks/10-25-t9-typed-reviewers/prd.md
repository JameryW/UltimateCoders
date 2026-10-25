# T9: typed reviewers fail-closed

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/09-typed-reviewers-fail-closed.md`
- Decision D5: align `FailureKind`; one reviewer per kind; no labels ⇒ kind off; no default 0.5
- Spec-p1 Seam E (interface half; CV artifacts = T10)

## Goal

1. New module e.g. `python/ultimate_coders/agent/typed_review.py` (or extend `review.py` carefully without breaking parse gate):
   - `TYPED_REVIEW_ENFORCE_ENV = "UC_TYPED_REVIEW_ENFORCE"`; `typed_review_enforce_enabled()` default off
   - Re-export / use `FailureKind` from `routing` — **no parallel enum names**
   - `TypedReviewOutcome(kind, enabled, passed, reasons, review_verified: bool)`
   - `labels_available(kind) -> bool` — true only if an in-memory/registry label set or artifact path is registered for that kind (T9: empty registry by default ⇒ all disabled)
   - `review_for_kind(kind, evidence: Mapping) -> TypedReviewOutcome` — if not enforce or no labels: `enabled=False`, `passed=False`, `review_verified=False`; never invent calibrated score 0.5
   - `apply_typed_review(kind, evidence) -> dict` helper that may set `review_verified=True` only when enforce on AND kind enabled AND reviewer passes (T10 will supply thresholds; T9 can treat “labels present” as enabling a fail-closed stub that requires explicit `evidence["typed_review_pass"]=True` for that kind — still no 0.5 default)

2. Keep `parse_review` JSON format gate unchanged when typed path off.

3. Optional wiring: when typed review passes, callers can feed `review_verified` into completion conditions; LLM `approved` alone still fails.

## Out of scope

CV threshold artifacts / loader (T10); labeling UI; changing FailureKind taxonomy.

## Tests

`tests/python/test_typed_reviewers.py`: flag default off; no labels ⇒ disabled; taxonomy uses routing FailureKind values; parse_review still works; no 0.5 constant as calibrated threshold in module; successful stub path can set review_verified for completion evidence.
