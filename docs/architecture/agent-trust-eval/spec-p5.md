# Spec P5 — Label corpus boundary

Blocked by: D12 (decided). Builds on the CV threshold artifact (P1 / T10 / D5).

## Goal

Make the "who labels, where it lives, no report-role leakage" boundary enforceable:
a label manifest validator plus a cross-check when a CV threshold artifact is loaded
with its manifest.

## What to build

1. **`python/ultimate_coders/agent/review_labels.py`** — `LabelManifest` + `load_label_manifest(path)`:
   - required: `schema_version` (= 1), `kind`, `labeler`, `labeled_at`, `n_labels`,
     `source_eval_roles`, `corpus_uri`;
   - `kind` must be a `FailureKind` value; `labeler` must not be an agent/worker/
     service principal; `corpus_uri` must carry a URI scheme;
   - `source_eval_roles` non-empty, subset of `{search, select}` — `report` refused
     with a message naming the leakage;
   - refusals are `ValueError`, matching the threshold loader's contract.
2. **`typed_review_thresholds.load_threshold_artifact(path, *, label_manifest=None)`**:
   - with a manifest: same `kind`, same `n_labels`, and `n_labels >= cv_folds * 2`;
   - without one: unchanged behaviour.
3. **`typed_review_thresholds.register_threshold(artifact, *, label_manifest=None)`**:
   - with a manifest: the same cross-checks as loading, and the kind is recorded as
     `manifest`;
   - under `UC_REVIEWER_LABEL_PROVENANCE` (default off): a registration with **no**
     manifest is refused, so a kind cannot open on an unbacked `n_labels` claim;
   - flag off: unchanged registration, recorded as `unverified`.
4. **`threshold_provenance(kind)`** — `manifest` / `unverified` / `None` for observability.
5. **Tests**: manifest validation (happy path, each refusal, report role, agent
   labeler, bad URI, bad schema), the artifact cross-checks (kind mismatch, count
   mismatch, structural floor, manifest-less path unchanged), and the registration
   gate.

## Acceptance criteria

- [ ] A valid manifest loads and exposes its fields.
- [ ] A `report` source role is refused; `search`/`select` are accepted.
- [ ] An agent/worker/service labeler is refused; a human labeler is accepted.
- [ ] A repo-relative `corpus_uri` (no scheme) is refused.
- [ ] Artifact + manifest cross-checks refuse kind/count mismatch and the sub-floor case.
- [ ] Loading an artifact without a manifest behaves exactly as today.
- [ ] Under `UC_REVIEWER_LABEL_PROVENANCE`, registering without a manifest is refused and
      the kind stays closed; with a matching manifest it is recorded as `manifest`.
- [ ] Flag off records `unverified` and registers exactly as today.

## Out of scope

- Label row schema, storage, or upload tooling
- Any decision-time reviewer behaviour
- Calibration quality numbers
