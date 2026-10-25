# Typed Review Spec

> Per-failure-kind typed reviewers (D5 / tickets 09–10): fail-closed, label- and
> CV-threshold-gated, aligned to routing `FailureKind`.

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/typed_review.py` and
`python/ultimate_coders/agent/typed_review_thresholds.py`.

Trigger: any change to the typed-review flag, label registry, CV threshold
loader/registry, `review_for_kind`, or mapping into completion
`review_verified`. Do **not** invent a parallel failure taxonomy; do **not**
ship a calibrated `0.5` default (thresholds come only from CV artifacts). Keep
`review.parse_review` JSON format gate unchanged when the typed path is off.

---

## 2. Signatures

```python
TYPED_REVIEW_ENFORCE_ENV = "UC_TYPED_REVIEW_ENFORCE"

@dataclass(frozen=True)
class TypedReviewOutcome:
    kind: str
    enabled: bool
    passed: bool
    reasons: list[str]
    review_verified: bool

@dataclass(frozen=True)
class ThresholdArtifact:
    kind: str
    metric: str
    threshold: float
    cv_folds: int
    n_labels: int
    generated_at: str

def typed_review_enforce_enabled() -> bool: ...
def register_labels(kind: FailureKind | str, labels: Sequence) -> None: ...
def clear_labels() -> None: ...
def labels_available(kind: FailureKind | str) -> bool: ...
def load_threshold_artifact(path: str | Path) -> ThresholdArtifact: ...
def register_threshold(artifact: ThresholdArtifact) -> None: ...
def clear_thresholds() -> None: ...
def threshold_available(kind: str) -> bool: ...
def threshold_for(kind: str) -> float | None: ...
def review_for_kind(kind: FailureKind | str, evidence: Mapping) -> TypedReviewOutcome: ...
def completion_evidence_from_outcome(outcome: TypedReviewOutcome) -> dict: ...
def apply_typed_review(kind: FailureKind | str, evidence: Mapping) -> dict: ...
```

`FailureKind` is imported from `ultimate_coders.agent.routing` — no parallel enum.

---

## 3. Contracts

1. **Flag default off.** `UC_TYPED_REVIEW_ENFORCE` is off unless truthy
   (`1` / `true` / `yes` / `on`), same parser as other UC enforce flags.
2. **Taxonomy alignment.** Kind values are `transient` / `auth` / `schema` /
   `unknown` from routing `FailureKind`.
3. **Empty registries by default.** `labels_available(kind)` and
   `threshold_available(kind)` are false until labels / a CV artifact are
   registered for that kind.
4. **No labels ⇒ disabled.** Enforce on but no labels → `enabled=False`,
   `passed=False`, `review_verified=False` (fail closed to format gate / human).
5. **Flag off ⇒ disabled.** Same disabled outcome; reason mentions the flag.
   Artifacts alone do not enable without the flag.
6. **Labels + CV threshold required to enable.** Enforce on with labels but
   **no** threshold artifact → still `enabled=False`. When both are present,
   `enabled=True`; pass only when `evidence.get("typed_review_score")` is a
   finite float `>=` the artifact threshold (strict). Missing / non-finite /
   non-numeric scores fail closed. No default calibrated score (never invent
   `0.5`).
7. **Artifact loader refuse.** `load_threshold_artifact` rejects missing files,
   corrupt JSON, missing keys, wrong types, unknown kind, non-finite threshold,
   threshold not in `(0, 1]`, `cv_folds < 2`, `n_labels < 1`, or
   `"calibrated_default": true`. A CV-measured threshold of `0.5` is allowed;
   claiming a calibrated default is not. Empty/missing artifact ⇒ kind stays
   closed.
8. **Completion evidence.** `completion_evidence_from_outcome` /
   `apply_typed_review` return `{"review_verified": True}` only when the
   outcome verified; LLM JSON `approved` alone still fails
   `review_approved` completion conditions.
9. **`parse_review` unchanged.** Typed path does not alter the JSON format gate
   in `review.py`.

---

## 4. Validation & Error Matrix

| Input | Behavior |
|---|---|
| Flag unset / falsey | enforce off; reviewer disabled |
| No labels for kind | disabled even if flag on + threshold present |
| Labels but no threshold artifact | disabled (T10) |
| Labels + threshold + score ≥ threshold | enabled, passed, `review_verified=True` |
| Labels + threshold + missing / low score | enabled, failed closed |
| Missing / corrupt / `calibrated_default` artifact | loader raises; kind stays closed |
| Empty `register_labels(..., [])` | removes labels for that kind |

---

## 5. Good / Base / Bad Cases

- **Good:** enforce on + labels + CV artifact + `typed_review_score >= threshold`
  → completion can use `review_verified`.
- **Base:** flag off, empty label registry, or no threshold artifact → disabled;
  `parse_review` still works.
- **Bad:** inventing a second FailureKind enum; shipping `DEFAULT_THRESHOLD=0.5`;
  treating LLM `approved` as `review_verified`; enabling a kind from labels alone.

---

## 6. Regenerating CV threshold artifacts (offline)

There is **no** labeling UI in this ticket. Label ownership remains a fog item;
thresholds are produced offline and checked in (or loaded at process start).

1. Collect a labeled set for one `FailureKind` (positive/negative review evidence).
   Record `n_labels` (≥ 1).
2. Run k-fold cross-validation (`cv_folds` ≥ 2) offline against the chosen metric
   (e.g. `f1`). Choose the operating threshold on the CV curve — do **not** invent
   a mid-score default.
3. Write one JSON artifact per kind:

   ```json
   {
     "kind": "schema",
     "metric": "f1",
     "threshold": 0.82,
     "cv_folds": 5,
     "n_labels": 40,
     "generated_at": "2026-10-25T00:00:00Z"
   }
   ```

4. Load with `load_threshold_artifact(path)` then `register_threshold(artifact)`.
   Never set `"calibrated_default": true`. When labels change, re-run steps 1–4
   and replace the artifact; kinds without a fresh artifact stay closed.

Example fixture (tests only): `tests/python/fixtures/reviewer_cv_threshold_schema.json`.

---

## 7. Tests Required

- `tests/python/test_typed_reviewers.py`: flag default; no-labels disabled;
  labels-without-threshold disabled; FailureKind taxonomy; parse_review still
  works; no 0.5 constant; score≥threshold sets `review_verified` for completion.
- `tests/python/test_reviewer_cv_thresholds.py`: load success; missing/corrupt
  refuse; kind without artifact disabled; score≥threshold passes; enforce off
  inert; no hardcoded calibrated 0.5 default.

---

## 8. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Parallel `ReviewFailureKind` enum | Import routing `FailureKind` |
| Default pass score 0.5 when unlabeled | Disable kind when no labels / no CV artifact |
| Labels alone enable the stub | Require labels **and** threshold artifact |
| LLM `approved` ⇒ `review_verified` | Only typed outcome sets verified |
| `calibrated_default: true` in artifact | Refuse; CV-measured thresholds only |
