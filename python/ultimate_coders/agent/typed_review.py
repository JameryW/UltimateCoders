"""Per-failure-kind typed reviewers (P1 / T9–T10 / D5): fail-closed.

Aligns reviewer kinds to :class:`~ultimate_coders.agent.routing.FailureKind`
(``transient`` / ``auth`` / ``schema`` / ``unknown``). A kind enables only when
enforce is on, labels are registered, **and** a CV threshold artifact is
registered. Pass requires ``evidence["typed_review_score"]`` to be a finite
float ``>=`` the loaded threshold. Never invents a calibrated mid-score default.

Product callers gate via ``UC_TYPED_REVIEW_ENFORCE`` (default off). Successful
typed review may populate completion evidence ``review_verified``; LLM review
JSON ``approved`` alone still fails completion.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ultimate_coders.agent.routing import FailureKind
from ultimate_coders.agent.typed_review_thresholds import (
    ThresholdArtifact,
    clear_thresholds,
    get_threshold_artifact,
    load_threshold_artifact,
    register_threshold,
    threshold_available,
    threshold_for,
)

TYPED_REVIEW_ENFORCE_ENV = "UC_TYPED_REVIEW_ENFORCE"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

#: In-process label registry: FailureKind value → non-empty label set.
#: Empty by default ⇒ every kind disabled until :func:`register_labels`.
_LABELS: dict[str, tuple[Any, ...]] = {}


@dataclass(frozen=True)
class TypedReviewOutcome:
    """Outcome of one per-kind typed review attempt."""

    kind: str
    enabled: bool
    passed: bool
    reasons: list[str] = field(default_factory=list)
    review_verified: bool = False


def typed_review_enforce_enabled() -> bool:
    """Whether typed-review enforce is on; default off."""
    raw = os.environ.get(TYPED_REVIEW_ENFORCE_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV


def _normalize_kind(kind: FailureKind | str) -> str:
    if isinstance(kind, FailureKind):
        return kind.value
    if isinstance(kind, str):
        text = kind.strip().lower()
        if not text:
            raise ValueError("kind must be a non-empty FailureKind value")
        return text
    raise TypeError(f"kind must be FailureKind or str, got {type(kind)!r}")


def register_labels(kind: FailureKind | str, labels: Sequence[Any]) -> None:
    """Register in-memory labels for ``kind``. Empty labels leave the kind disabled."""
    key = _normalize_kind(kind)
    items = tuple(labels)
    if not items:
        _LABELS.pop(key, None)
        return
    _LABELS[key] = items


def clear_labels() -> None:
    """Clear all registered labels (test / reset helper)."""
    _LABELS.clear()


def labels_available(kind: FailureKind | str) -> bool:
    """True only when a non-empty label set is registered for ``kind``."""
    key = _normalize_kind(kind)
    return bool(_LABELS.get(key))


def _score_meets_threshold(score: Any, threshold: float) -> bool:
    """True when ``score`` is a finite number (not bool) ``>= threshold``."""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    value = float(score)
    return math.isfinite(value) and value >= threshold


def review_for_kind(
    kind: FailureKind | str,
    evidence: Mapping[str, Any],
) -> TypedReviewOutcome:
    """Run the fail-closed CV-threshold reviewer for one :class:`FailureKind`.

    Enablement requires enforce on, labels registered, **and** a threshold
    artifact registered for the kind. When enabled, passes only if
    ``evidence.get("typed_review_score")`` is a finite float ``>=`` the
    artifact threshold. Missing/non-finite scores fail closed. Never invents
    a calibrated default threshold.
    """
    key = _normalize_kind(kind)
    if not typed_review_enforce_enabled():
        return TypedReviewOutcome(
            kind=key,
            enabled=False,
            passed=False,
            reasons=[f"{TYPED_REVIEW_ENFORCE_ENV} is off"],
            review_verified=False,
        )
    if not labels_available(key):
        return TypedReviewOutcome(
            kind=key,
            enabled=False,
            passed=False,
            reasons=[f"no labels registered for kind={key!r}; reviewer disabled"],
            review_verified=False,
        )
    artifact = get_threshold_artifact(key)
    if artifact is None:
        return TypedReviewOutcome(
            kind=key,
            enabled=False,
            passed=False,
            reasons=[f"no CV threshold artifact registered for kind={key!r}; reviewer disabled"],
            review_verified=False,
        )
    threshold = artifact.threshold
    score = evidence.get("typed_review_score")
    if _score_meets_threshold(score, threshold):
        return TypedReviewOutcome(
            kind=key,
            enabled=True,
            passed=True,
            reasons=[f"typed_review_score {float(score)} >= threshold {threshold}"],
            review_verified=True,
        )
    return TypedReviewOutcome(
        kind=key,
        enabled=True,
        passed=False,
        reasons=[f"typed_review_score {score!r} does not meet threshold {threshold} (fail closed)"],
        review_verified=False,
    )


def completion_evidence_from_outcome(outcome: TypedReviewOutcome) -> dict[str, Any]:
    """Map a typed-review outcome into completion evidence.

    Returns ``{"review_verified": True}`` only when ``outcome.review_verified``;
    otherwise an empty dict (callers must not treat LLM ``approved`` as verified).
    """
    if outcome.review_verified:
        return {"review_verified": True}
    return {}


def apply_typed_review(
    kind: FailureKind | str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Run :func:`review_for_kind` and return completion evidence when verified."""
    return completion_evidence_from_outcome(review_for_kind(kind, evidence))


__all__ = [
    "TYPED_REVIEW_ENFORCE_ENV",
    "ThresholdArtifact",
    "TypedReviewOutcome",
    "apply_typed_review",
    "clear_labels",
    "clear_thresholds",
    "completion_evidence_from_outcome",
    "get_threshold_artifact",
    "labels_available",
    "load_threshold_artifact",
    "register_labels",
    "register_threshold",
    "review_for_kind",
    "threshold_available",
    "threshold_for",
    "typed_review_enforce_enabled",
]
