"""AnyS1: a calibrated System-1 pointer router for workflow decision points.

Ledger item #121 (arXiv:2611.23751). A small, deterministic library: the
router consumes pre-computed candidate actions and their model confidences,
label-smooths them against observed outcomes, and only escalates to a
System-2 caller when the calibrated confidence falls below the threshold.
It never calls a model, reads a file, or mutates a workspace.

Feature flag: ``UC_ANYS1_ROUTER`` (default off; the flag gates callers, not
imports).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "ANYS1_ROUTER_ENV",
    "AnyS1Router",
    "CalibrationStats",
    "DecisionOption",
    "RouteDecision",
    "anys1_router_enabled",
]

ANYS1_ROUTER_ENV = "UC_ANYS1_ROUTER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def anys1_router_enabled() -> bool:
    return os.environ.get(ANYS1_ROUTER_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class DecisionOption:
    """One candidate action at a decision point."""

    action: str
    confidence: float
    cost: float = 0.0

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise ValueError("action must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.cost < 0.0:
            raise ValueError("cost must be non-negative")


@dataclass(frozen=True)
class RouteDecision:
    """The router's choice and whether a System-2 caller must confirm it."""

    action: str
    confidence: float
    system: int
    escalated: bool
    reason: str


@dataclass(frozen=True)
class CalibrationStats:
    observations: int
    accuracy: float
    mean_confidence: float
    calibration_error: float

    def to_dict(self) -> dict[str, object]:
        return {
            "observations": self.observations,
            "accuracy": round(self.accuracy, 6),
            "mean_confidence": round(self.mean_confidence, 6),
            "calibration_error": round(self.calibration_error, 6),
        }


class AnyS1Router:
    """Confidence-gated pointer router with simple label-smoothing calibration."""

    def __init__(self, confidence_threshold: float = 0.8) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in [0, 1]")
        self.confidence_threshold = confidence_threshold
        self._predicted: list[float] = []
        self._outcomes: list[bool] = []

    @property
    def observations(self) -> int:
        return len(self._outcomes)

    def observe(self, confidence: float, correct: bool) -> None:
        """Record an outcome for calibration; no-op when disabled by caller."""
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        self._predicted.append(confidence)
        self._outcomes.append(bool(correct))

    def calibrate(self, raw_confidence: float) -> float:
        """Label-smooth a raw confidence toward the observed accuracy.

        With fewer than three observations the raw value is returned. With
        three or more, the accuracy prior contributes one third of the score.
        """
        if not 0.0 <= raw_confidence <= 1.0:
            raise ValueError("raw_confidence must be in [0, 1]")
        if len(self._outcomes) < 3:
            return raw_confidence
        accuracy = sum(self._outcomes) / len(self._outcomes)
        return round((2.0 * raw_confidence + accuracy) / 3.0, 6)

    def route(self, options: Iterable[DecisionOption]) -> RouteDecision:
        """Pick the highest-confidence option and gate it on calibration."""
        candidates = list(options)
        if not candidates:
            raise ValueError("at least one decision option is required")
        best = min(candidates, key=lambda option: (-option.confidence, option.cost, option.action))
        calibrated = self.calibrate(best.confidence)
        if calibrated >= self.confidence_threshold:
            return RouteDecision(
                action=best.action,
                confidence=calibrated,
                system=1,
                escalated=False,
                reason="calibrated_confidence",
            )
        return RouteDecision(
            action=best.action,
            confidence=calibrated,
            system=2,
            escalated=True,
            reason="below_confidence_threshold",
        )

    def stats(self) -> CalibrationStats:
        if not self._outcomes:
            return CalibrationStats(0, 0.0, 0.0, 0.0)
        accuracy = sum(self._outcomes) / len(self._outcomes)
        mean_confidence = sum(self._predicted) / len(self._predicted)
        error = sum(abs(p - float(o)) for p, o in zip(self._predicted, self._outcomes)) / len(
            self._outcomes
        )
        return CalibrationStats(len(self._outcomes), accuracy, mean_confidence, error)

    def reset(self) -> None:
        self._predicted.clear()
        self._outcomes.clear()
