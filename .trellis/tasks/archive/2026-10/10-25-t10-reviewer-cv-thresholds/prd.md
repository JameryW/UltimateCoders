# T10: reviewer CV thresholds

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/10-reviewer-cv-thresholds.md`
- Depends on T9 (`typed_review.py` / FailureKind reviewers)
- Decision D5 / spec-p1 Seam E (threshold half)

## Goal

1. **Artifact schema** (JSON file per kind or one multi-kind file), e.g.:
   ```json
   {"kind": "schema", "metric": "f1", "threshold": 0.82, "cv_folds": 5, "n_labels": 40, "generated_at": "..."}
   ```
   Threshold must be a float in `(0, 1]` produced by CV — loader rejects missing fields, wrong kind, non-finite, or the forbidden sentinel `0.5` *as the only claimed “calibrated default”* — actually ticket says don’t ship 0.5 as calibrated default; a real CV artifact may legitimately contain 0.5 if measured. Prefer: reject artifacts that set `"calibrated_default": true` or omit `n_labels`/`cv_folds`; allow any finite threshold in (0,1] from a real artifact. Document that empty/missing artifact ⇒ kind stays closed.

2. **Loader** in `typed_review.py` or `typed_review_thresholds.py`:
   - `load_threshold_artifact(path) -> ThresholdArtifact`
   - `register_threshold(kind, artifact)` / `clear_thresholds()` / `threshold_available(kind)`
   - Missing/corrupt file → refuse (kind stays disabled even if labels registered)
   - `threshold_for(kind) -> float | None`

3. **Integrate into `review_for_kind`:** when enforce on AND labels available AND threshold available: pass iff `evidence.get("typed_review_score")` is a finite float `>= threshold` (strict). If labels but no threshold artifact → still disabled (or keep T9 stub only when explicitly opted — prefer: **require threshold artifact to enable** once T10 lands, so no magic defaults). If score missing → fail closed.

4. **Docs:** short regeneration note in typed-review-spec (how to produce artifact from labels offline; no labeling UI).

5. Flag still default off; artifacts alone do not enable without `UC_TYPED_REVIEW_ENFORCE`.

## Out of scope

Labeling UI; GPU training; changing FailureKind; Hy-Memory (T11/T12).

## Tests

`tests/python/test_reviewer_cv_thresholds.py`: load success; missing/corrupt refuse; kind without artifact disabled; score≥threshold passes; enforce off still inert; no hardcoded calibrated 0.5 default in code.
