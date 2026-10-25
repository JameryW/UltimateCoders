"""CV threshold artifacts for typed reviewers (P1 / T10 / D5).

Thresholds load only from checked-in (or offline-generated) JSON artifacts.
Missing, corrupt, or ``calibrated_default: true`` artifacts are refused; kinds
without a registered artifact stay closed. Never invent a mid-score default.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ultimate_coders.agent.routing import FailureKind

#: Allowed FailureKind string values (no parallel taxonomy).
_VALID_KINDS = frozenset(k.value for k in FailureKind)

#: Required top-level keys on a per-kind CV artifact.
_REQUIRED_KEYS = frozenset({"kind", "metric", "threshold", "cv_folds", "n_labels", "generated_at"})


@dataclass(frozen=True)
class ThresholdArtifact:
    """One offline CV threshold for a single :class:`FailureKind`."""

    kind: str
    metric: str
    threshold: float
    cv_folds: int
    n_labels: int
    generated_at: str


#: In-process threshold registry: FailureKind value → artifact.
_THRESHOLDS: dict[str, ThresholdArtifact] = {}


def _refuse(message: str) -> None:
    raise ValueError(message)


def _require_mapping(data: Any, *, source: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        _refuse(f"threshold artifact must be a JSON object ({source})")
    return data


def _parse_artifact(data: Mapping[str, Any], *, source: str) -> ThresholdArtifact:
    missing = sorted(_REQUIRED_KEYS - set(data))
    if missing:
        _refuse(f"threshold artifact missing keys {missing} ({source})")

    if data.get("calibrated_default") is True:
        _refuse(
            "threshold artifact must not set calibrated_default=true "
            f"({source}); CV-measured thresholds only"
        )

    kind_raw = data["kind"]
    if not isinstance(kind_raw, str) or not kind_raw.strip():
        _refuse(f"threshold artifact kind must be a non-empty string ({source})")
    kind = kind_raw.strip().lower()
    if kind not in _VALID_KINDS:
        _refuse(
            f"threshold artifact kind={kind_raw!r} is not a FailureKind "
            f"value {sorted(_VALID_KINDS)} ({source})"
        )

    metric = data["metric"]
    if not isinstance(metric, str) or not metric.strip():
        _refuse(f"threshold artifact metric must be a non-empty string ({source})")

    threshold_raw = data["threshold"]
    if isinstance(threshold_raw, bool) or not isinstance(threshold_raw, (int, float)):
        _refuse(f"threshold artifact threshold must be a number ({source})")
    threshold = float(threshold_raw)
    if not math.isfinite(threshold):
        _refuse(f"threshold artifact threshold must be finite ({source})")
    if not (0.0 < threshold <= 1.0):
        _refuse(f"threshold artifact threshold must be in (0, 1], got {threshold} ({source})")

    cv_folds_raw = data["cv_folds"]
    if isinstance(cv_folds_raw, bool) or not isinstance(cv_folds_raw, int):
        _refuse(f"threshold artifact cv_folds must be an int ({source})")
    if cv_folds_raw < 2:
        _refuse(f"threshold artifact cv_folds must be >= 2, got {cv_folds_raw} ({source})")

    n_labels_raw = data["n_labels"]
    if isinstance(n_labels_raw, bool) or not isinstance(n_labels_raw, int):
        _refuse(f"threshold artifact n_labels must be an int ({source})")
    if n_labels_raw < 1:
        _refuse(f"threshold artifact n_labels must be >= 1, got {n_labels_raw} ({source})")

    generated_at = data["generated_at"]
    if not isinstance(generated_at, str) or not generated_at.strip():
        _refuse(f"threshold artifact generated_at must be a non-empty string ({source})")

    return ThresholdArtifact(
        kind=kind,
        metric=metric.strip(),
        threshold=threshold,
        cv_folds=cv_folds_raw,
        n_labels=n_labels_raw,
        generated_at=generated_at.strip(),
    )


def load_threshold_artifact(path: str | Path) -> ThresholdArtifact:
    """Load and validate one per-kind CV threshold JSON artifact.

    Refuses (raises :class:`ValueError`) on missing file, corrupt JSON, missing
    fields, wrong types, non-finite / out-of-range threshold, ``cv_folds < 2``,
    ``n_labels < 1``, unknown ``kind``, or ``calibrated_default: true``.
    A real CV-measured threshold of ``0.5`` is allowed; claiming a calibrated
    default is not.
    """
    artifact_path = Path(path)
    if not artifact_path.is_file():
        _refuse(f"threshold artifact not found: {artifact_path}")
    try:
        raw_text = artifact_path.read_text(encoding="utf-8")
    except OSError as exc:
        _refuse(f"threshold artifact unreadable: {artifact_path}: {exc}")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        _refuse(f"threshold artifact corrupt JSON ({artifact_path}): {exc}")
    source = str(artifact_path)
    return _parse_artifact(_require_mapping(data, source=source), source=source)


def _registry_key(kind: str) -> str:
    """Normalize a registry lookup key (lowercase strip); empty stays empty."""
    if not isinstance(kind, str):
        raise TypeError(f"kind must be str, got {type(kind)!r}")
    return kind.strip().lower()


def register_threshold(artifact: ThresholdArtifact) -> None:
    """Register a validated CV threshold for ``artifact.kind``.

    Kind is normalized to a FailureKind value. Invalid kinds are refused so a
    hand-built artifact cannot open a parallel taxonomy in the registry.
    """
    if not isinstance(artifact, ThresholdArtifact):
        raise TypeError(f"artifact must be ThresholdArtifact, got {type(artifact)!r}")
    kind = _registry_key(artifact.kind)
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"threshold artifact kind={artifact.kind!r} is not a FailureKind "
            f"value {sorted(_VALID_KINDS)}"
        )
    if kind != artifact.kind:
        artifact = ThresholdArtifact(
            kind=kind,
            metric=artifact.metric,
            threshold=artifact.threshold,
            cv_folds=artifact.cv_folds,
            n_labels=artifact.n_labels,
            generated_at=artifact.generated_at,
        )
    _THRESHOLDS[kind] = artifact


def clear_thresholds() -> None:
    """Clear all registered thresholds (test / reset helper)."""
    _THRESHOLDS.clear()


def threshold_available(kind: str) -> bool:
    """True when a CV artifact is registered for ``kind`` (normalized value)."""
    return _registry_key(kind) in _THRESHOLDS


def threshold_for(kind: str) -> float | None:
    """Return the registered threshold float for ``kind``, or ``None``."""
    artifact = _THRESHOLDS.get(_registry_key(kind))
    if artifact is None:
        return None
    return artifact.threshold


def get_threshold_artifact(kind: str) -> ThresholdArtifact | None:
    """Return the registered artifact for ``kind``, or ``None``."""
    return _THRESHOLDS.get(_registry_key(kind))
