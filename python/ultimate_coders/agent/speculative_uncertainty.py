"""Speculative Uncertainty: black-box pre-execution veto gate (#35).

Ledger item #35 adapts Speculative Uncertainty (arXiv:2609.05274).  LatentSift
needs the generator's hidden states, which closed Adapter APIs never expose.
This module instead consumes cross-likelihood summaries that a small local
draft model can compute in one forward pass over the already-generated
trajectory.  Reasoning and action spans are kept separate because a confident
rationale does not imply a safe tool call.

The gate is stage-aware and purely arithmetic; it does not call a model.

Feature flag: ``UC_SPECULATIVE_UNCERTAINTY`` (default off).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "UC_SPECULATIVE_UNCERTAINTY",
    "SpanKind",
    "SpanLikelihood",
    "UncertaintyFeatures",
    "UncertaintyAssessment",
    "SpeculativeUncertaintyGate",
    "speculative_uncertainty_enabled",
    "assess_uncertainty",
]

UC_SPECULATIVE_UNCERTAINTY = "UC_SPECULATIVE_UNCERTAINTY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_DEFAULT_WEIGHTS = (0.4, 0.6)
_STAGE_WEIGHTS = {
    "localization": (0.3, 0.7),
    "editing": (0.45, 0.55),
    "verification": (0.5, 0.5),
    "repair": (0.55, 0.45),
}


def speculative_uncertainty_enabled() -> bool:
    """Return whether the black-box uncertainty gate is enabled."""

    return os.environ.get(UC_SPECULATIVE_UNCERTAINTY, "").strip().lower() in _TRUTHY


class SpanKind(str, Enum):
    REASONING = "reasoning"
    ACTION = "action"


@dataclass(frozen=True)
class SpanLikelihood:
    """Mean token log-likelihood for one span, as reported by the draft model."""

    kind: SpanKind
    token_count: int
    mean_logprob: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_count", max(0, int(self.token_count)))
        value = float(self.mean_logprob)
        if not math.isfinite(value):
            raise ValueError("mean_logprob must be finite")
        object.__setattr__(self, "mean_logprob", min(0.0, value))

    @property
    def surprise(self) -> float:
        return max(0.0, -self.mean_logprob)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "token_count": self.token_count,
            "mean_logprob": round(self.mean_logprob, 6),
            "surprise": round(self.surprise, 6),
        }


@dataclass(frozen=True)
class UncertaintyFeatures:
    """Stage-aware cross-likelihood features for one candidate action."""

    reasoning: SpanLikelihood
    action: SpanLikelihood
    stage: str = "editing"

    def __post_init__(self) -> None:
        if self.reasoning.kind is not SpanKind.REASONING:
            raise ValueError("reasoning span must have kind reasoning")
        if self.action.kind is not SpanKind.ACTION:
            raise ValueError("action span must have kind action")
        object.__setattr__(self, "stage", str(self.stage).strip().lower())

    def to_dict(self) -> dict[str, object]:
        return {
            "reasoning": self.reasoning.to_dict(),
            "action": self.action.to_dict(),
            "stage": self.stage,
        }


@dataclass(frozen=True)
class UncertaintyAssessment:
    risk: float
    calibrated_probability: float
    veto: bool
    reason: str
    stage: str
    weights: tuple[float, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "risk": round(self.risk, 6),
            "calibrated_probability": round(self.calibrated_probability, 6),
            "veto": self.veto,
            "reason": self.reason,
            "stage": self.stage,
            "weights": [round(value, 6) for value in self.weights],
        }


class SpeculativeUncertaintyGate:
    """A calibrated veto gate for adapters that expose no logits."""

    def __init__(
        self,
        *,
        threshold: float = 0.6,
        stage_weights: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [0, 1]")
        self.threshold = threshold
        self.stage_weights = dict(_STAGE_WEIGHTS)
        if stage_weights:
            self.stage_weights.update(stage_weights)

    def weights_for(self, stage: str) -> tuple[float, float]:
        return self.stage_weights.get(stage, _DEFAULT_WEIGHTS)

    def assess(self, features: UncertaintyFeatures) -> UncertaintyAssessment:
        reasoning_weight, action_weight = self.weights_for(features.stage)
        total_weight = reasoning_weight + action_weight
        weighted_surprise = (
            reasoning_weight * features.reasoning.surprise
            + action_weight * features.action.surprise
        ) / total_weight
        risk = 1.0 - math.exp(-weighted_surprise)
        probability = 1.0 / (1.0 + math.exp(-(weighted_surprise - 1.0)))
        veto = features.action.token_count > 0 and risk >= self.threshold
        if veto:
            reason = (
                f"action-span risk {risk:.3f} is at or above the veto threshold "
                f"{self.threshold:.3f}"
            )
        elif features.action.token_count == 0:
            reason = "no action span was generated; nothing to veto"
        else:
            reason = f"action-span risk {risk:.3f} is below the veto threshold"
        return UncertaintyAssessment(
            risk=round(risk, 6),
            calibrated_probability=round(probability, 6),
            veto=veto,
            reason=reason,
            stage=features.stage,
            weights=(reasoning_weight, action_weight),
        )

    def should_veto(self, features: UncertaintyFeatures) -> bool:
        return self.assess(features).veto


def assess_uncertainty(
    features: UncertaintyFeatures,
    *,
    threshold: float = 0.6,
) -> UncertaintyAssessment:
    """Convenience wrapper for a one-shot assessment."""

    return SpeculativeUncertaintyGate(threshold=threshold).assess(features)
