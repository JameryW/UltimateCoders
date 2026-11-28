"""Adversarial speculative reasoning for long-horizon agents (ledger item #106).

A small draft model is tuned on a tiny fraction of its parameters so that it
deliberately triggers a target-model rejection and restart at critical reasoning
forks. The target model itself is never changed; its key thinking tokens are
simply resampled at the lowest numerical-noise point.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "ADV_SPEC_REASONER_ENV",
    "DraftModel",
    "ReasoningFork",
    "RestartDecision",
    "ForkPlan",
    "AdversarialSpeculativeReasoner",
    "adv_spec_reasoner_enabled",
]

ADV_SPEC_REASONER_ENV = "UC_ADV_SPEC_REASONER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MIN_TRAINABLE_FRACTION = 0.0006
_MAX_TRAINABLE_FRACTION = 0.0028


def adv_spec_reasoner_enabled() -> bool:
    return os.environ.get(ADV_SPEC_REASONER_ENV, "").strip().lower() in _TRUTHY


class AdvSpecReasonerError(ValueError):
    pass


@dataclass(frozen=True)
class DraftModel:
    name: str
    total_parameters: int
    trainable_fraction: float = 0.001

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise AdvSpecReasonerError("name must not be empty")
        if self.total_parameters <= 0:
            raise AdvSpecReasonerError("total_parameters must be positive")
        if not _MIN_TRAINABLE_FRACTION <= self.trainable_fraction <= _MAX_TRAINABLE_FRACTION:
            raise AdvSpecReasonerError("trainable_fraction outside published range")

    @property
    def trainable_parameters(self) -> int:
        return max(1, round(self.total_parameters * self.trainable_fraction))


@dataclass(frozen=True)
class ReasoningFork:
    step: int
    entropy: float
    target_confidence: float
    critical: bool = True

    def __post_init__(self) -> None:
        if self.step < 0:
            raise AdvSpecReasonerError("step must be non-negative")
        if self.entropy < 0:
            raise AdvSpecReasonerError("entropy must be non-negative")
        if not 0.0 <= self.target_confidence <= 1.0:
            raise AdvSpecReasonerError("target_confidence must be in [0, 1]")


@dataclass(frozen=True)
class RestartDecision:
    step: int
    restart: bool
    reason: str


@dataclass(frozen=True)
class ForkPlan:
    decisions: tuple[RestartDecision, ...]
    restart_steps: tuple[int, ...]
    draft_trainable_parameters: int


class AdversarialSpeculativeReasoner:
    def __init__(self, entropy_threshold: float = 1.0, target_confidence: float = 0.6) -> None:
        if entropy_threshold <= 0:
            raise AdvSpecReasonerError("entropy_threshold must be positive")
        if not 0.0 <= target_confidence <= 1.0:
            raise AdvSpecReasonerError("target_confidence must be in [0, 1]")
        self.entropy_threshold = entropy_threshold
        self.target_confidence = target_confidence

    def should_restart(self, fork: ReasoningFork) -> RestartDecision:
        restart = (
            fork.critical
            and fork.entropy >= self.entropy_threshold
            and fork.target_confidence < self.target_confidence
        )
        if restart:
            reason = "critical low-confidence fork: force target resample"
        elif not fork.critical:
            reason = "non-critical fork: keep draft token"
        else:
            reason = "fork below adversarial threshold"
        return RestartDecision(step=fork.step, restart=restart, reason=reason)

    def plan(
        self,
        model: DraftModel,
        forks: tuple[ReasoningFork, ...] | list[ReasoningFork],
    ) -> ForkPlan:
        decisions = tuple(self.should_restart(fork) for fork in forks)
        return ForkPlan(
            decisions=decisions,
            restart_steps=tuple(decision.step for decision in decisions if decision.restart),
            draft_trainable_parameters=model.trainable_parameters,
        )

    def density(self, model: DraftModel) -> float:
        return model.trainable_fraction
