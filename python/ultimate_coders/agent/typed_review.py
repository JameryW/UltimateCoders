"""Per-failure-kind typed reviewers (P1 / T9 / D5): fail-closed, labels-gated.

Aligns reviewer kinds to :class:`~ultimate_coders.agent.routing.FailureKind`
(``transient`` / ``auth`` / ``schema`` / ``unknown``). Without registered
labels for a kind, that reviewer stays disabled. Thresholds from cross-
validation land in T10; this module ships a fail-closed stub that requires
explicit ``evidence["typed_review_pass"] is True`` and never invents a
calibrated mid-score default.

Product callers gate via ``UC_TYPED_REVIEW_ENFORCE`` (default off). Successful
typed review may populate completion evidence ``review_verified``; LLM review
JSON ``approved`` alone still fails completion.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ultimate_coders.agent.routing import FailureKind

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


def review_for_kind(
    kind: FailureKind | str,
    evidence: Mapping[str, Any],
) -> TypedReviewOutcome:
    """Run the fail-closed stub reviewer for one :class:`FailureKind`.

    When enforce is off or no labels are registered for the kind, returns
    ``enabled=False``, ``passed=False``, ``review_verified=False``. When
    labels are present, passes only if ``evidence.get("typed_review_pass")
    is True`` (strict identity). Never invents a calibrated score threshold.
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
    # Labels present ⇒ kind enabled. Stub until T10 CV thresholds: require
    # explicit boolean evidence, never a default calibrated score.
    if evidence.get("typed_review_pass") is True:
        return TypedReviewOutcome(
            kind=key,
            enabled=True,
            passed=True,
            reasons=["typed_review_pass is True"],
            review_verified=True,
        )
    return TypedReviewOutcome(
        kind=key,
        enabled=True,
        passed=False,
        reasons=["typed_review_pass is not True (fail closed)"],
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
