# Typed Review Spec

> Per-failure-kind typed reviewers (D5 / ticket 09): fail-closed, label-gated,
> aligned to routing `FailureKind`. CV thresholds are T10.

---

## 1. Scope / Trigger

Module: `python/ultimate_coders/agent/typed_review.py`.

Trigger: any change to the typed-review flag, label registry, `review_for_kind`,
or mapping into completion `review_verified`. Do **not** invent a parallel
failure taxonomy; do **not** commit a calibrated 0.5 threshold here (T10 owns
CV artifacts). Keep `review.parse_review` JSON format gate unchanged when the
typed path is off.

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

def typed_review_enforce_enabled() -> bool: ...
def register_labels(kind: FailureKind | str, labels: Sequence) -> None: ...
def clear_labels() -> None: ...
def labels_available(kind: FailureKind | str) -> bool: ...
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
3. **Empty registry by default.** `labels_available(kind)` is false until
   `register_labels` stores a non-empty set for that kind.
4. **No labels ⇒ disabled.** Enforce on but no labels → `enabled=False`,
   `passed=False`, `review_verified=False` (fail closed to format gate / human).
5. **Flag off ⇒ disabled.** Same disabled outcome; reason mentions the flag.
6. **Labels present ⇒ stub gate.** `enabled=True`; pass only when
   `evidence.get("typed_review_pass") is True` (strict identity). On pass,
   `review_verified=True`. No default calibrated score (never `0.5`).
7. **Completion evidence.** `completion_evidence_from_outcome` /
   `apply_typed_review` return `{"review_verified": True}` only when the
   outcome verified; LLM JSON `approved` alone still fails
   `review_approved` completion conditions.
8. **`parse_review` unchanged.** Typed path does not alter the JSON format gate
   in `review.py`.

---

## 4. Validation & Error Matrix

| Input | Behavior |
|---|---|
| Flag unset / falsey | enforce off; reviewer disabled |
| No labels for kind | disabled even if flag on |
| Labels + `typed_review_pass is True` | enabled, passed, `review_verified=True` |
| Labels + missing / non-`True` pass | enabled, failed closed |
| Empty `register_labels(..., [])` | removes labels for that kind |

---

## 5. Good / Base / Bad Cases

- **Good:** enforce on + labels + `typed_review_pass=True` → completion can use
  `review_verified`.
- **Base:** flag off or empty registry → disabled; `parse_review` still works.
- **Bad:** inventing a second FailureKind enum; shipping `DEFAULT_THRESHOLD=0.5`;
  treating LLM `approved` as `review_verified`.

---

## 6. Tests Required

`tests/python/test_typed_reviewers.py`: flag default; no-labels disabled;
FailureKind taxonomy; parse_review still works; no 0.5 constant; stub path sets
`review_verified` for completion.

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Parallel `ReviewFailureKind` enum | Import routing `FailureKind` |
| Default pass score 0.5 when unlabeled | Disable kind when no labels |
| LLM `approved` ⇒ `review_verified` | Only typed outcome sets verified |
