"""ANTE world model: anticipate-then-execute with belief-anchored decisions (#28).

Ledger item #28 adapts ANTE (arXiv:2610.15368).  Before each tool action the
worker explicitly predicts two things — the expected exit status and the
expected observation digest — plus a trust/abstain decision.  Predictions are
scored after execution and their reward is anchored into the policy advantage
(BAPO); at inference time candidate actions can be ranked by predicted utility
without any external scorer.

Feature flag: ``UC_ANTE_WORLD_MODEL`` (default off).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "UC_ANTE_WORLD_MODEL",
    "ConfidenceDecision",
    "ActionPrediction",
    "PredictionOutcome",
    "PredictionEvaluation",
    "CandidateAction",
    "AnteWorldModel",
    "ante_world_model_enabled",
    "bapo_advantage",
]

UC_ANTE_WORLD_MODEL = "UC_ANTE_WORLD_MODEL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def ante_world_model_enabled() -> bool:
    """Return whether the ANTE belief world model is enabled."""

    return os.environ.get(UC_ANTE_WORLD_MODEL, "").strip().lower() in _TRUTHY


class ConfidenceDecision(str, Enum):
    TRUST = "trust"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class ActionPrediction:
    """The prediction fields a worker emits before executing an action."""

    predicted_exit_code: int
    predicted_observation_digest: str
    confidence_decision: ConfidenceDecision = ConfidenceDecision.ABSTAIN

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "predicted_exit_code", int(self.predicted_exit_code)
        )
        object.__setattr__(
            self,
            "predicted_observation_digest",
            str(self.predicted_observation_digest).strip(),
        )
        if isinstance(self.confidence_decision, str):
            object.__setattr__(
                self,
                "confidence_decision",
                ConfidenceDecision(self.confidence_decision),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "predicted_exit_code": self.predicted_exit_code,
            "predicted_observation_digest": self.predicted_observation_digest,
            "confidence_decision": self.confidence_decision.value,
        }


@dataclass(frozen=True)
class PredictionOutcome:
    exit_code: int
    observation_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "exit_code", int(self.exit_code))
        object.__setattr__(
            self, "observation_digest", str(self.observation_digest).strip()
        )


@dataclass(frozen=True)
class PredictionEvaluation:
    action_id: str
    prediction: ActionPrediction
    outcome: PredictionOutcome
    exit_match: bool
    digest_match: bool
    reward: float
    utility: float
    trusted: bool

    @property
    def correct(self) -> bool:
        return self.exit_match and self.digest_match

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "prediction": self.prediction.to_dict(),
            "outcome": {
                "exit_code": self.outcome.exit_code,
                "observation_digest": self.outcome.observation_digest,
            },
            "exit_match": self.exit_match,
            "digest_match": self.digest_match,
            "reward": round(self.reward, 6),
            "utility": round(self.utility, 6),
            "trusted": self.trusted,
        }


@dataclass(frozen=True)
class CandidateAction:
    candidate_id: str
    expected_utility: float
    confidence_decision: ConfidenceDecision = ConfidenceDecision.ABSTAIN
    predicted_accuracy: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise ValueError("candidate_id must be non-empty")
        object.__setattr__(self, "candidate_id", str(self.candidate_id).strip())
        if isinstance(self.confidence_decision, str):
            object.__setattr__(
                self,
                "confidence_decision",
                ConfidenceDecision(self.confidence_decision),
            )


@dataclass
class AnteWorldModel:
    """Small deterministic world model over tool-action predictions."""

    target_accuracy: float = 0.8
    penalty_alpha: float = 1.0
    prior_accuracy: float = 0.5
    max_records: int = 512
    _pending: dict[str, ActionPrediction] = field(init=False, default_factory=dict)
    _records: list[PredictionEvaluation] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if not 0.0 < self.target_accuracy < 1.0:
            raise ValueError("target_accuracy must be in (0, 1)")
        if self.penalty_alpha < 0.0:
            raise ValueError("penalty_alpha must be >= 0")
        if not 0.0 <= self.prior_accuracy <= 1.0:
            raise ValueError("prior_accuracy must be in [0, 1]")
        if self.max_records < 1:
            raise ValueError("max_records must be >= 1")

    @property
    def penalty_gamma(self) -> float:
        """`gamma = alpha * q* / (1 - q*)` from ANTE."""

        return self.penalty_alpha * self.target_accuracy / (1.0 - self.target_accuracy)

    @property
    def records(self) -> tuple[PredictionEvaluation, ...]:
        return tuple(self._records)

    def estimate_accuracy(self) -> float:
        """Empirical field-level prediction accuracy (prior when empty)."""

        if not self._records:
            return self.prior_accuracy
        hits = sum(
            int(record.exit_match) + int(record.digest_match)
            for record in self._records
        )
        return hits / (2.0 * len(self._records))

    def should_trust(self, estimated_accuracy: float | None = None) -> bool:
        accuracy = self.estimate_accuracy() if estimated_accuracy is None else estimated_accuracy
        return accuracy > self.target_accuracy

    def register(self, action_id: str, prediction: ActionPrediction) -> None:
        action_id = str(action_id).strip()
        if not action_id:
            raise ValueError("action_id must be non-empty")
        self._pending[action_id] = prediction

    def observe(self, action_id: str, outcome: PredictionOutcome) -> PredictionEvaluation:
        action_id = str(action_id).strip()
        prediction = self._pending.pop(action_id, None)
        if prediction is None:
            raise KeyError(f"no pending prediction for action {action_id!r}")
        exit_match = prediction.predicted_exit_code == outcome.exit_code
        digest_match = (
            prediction.predicted_observation_digest == outcome.observation_digest
        )
        correct = exit_match and digest_match
        reward = 1.0 if correct else 0.0
        trusted = prediction.confidence_decision is ConfidenceDecision.TRUST
        utility = reward
        if trusted and not correct:
            utility -= self.penalty_gamma
        evaluation = PredictionEvaluation(
            action_id=action_id,
            prediction=prediction,
            outcome=outcome,
            exit_match=exit_match,
            digest_match=digest_match,
            reward=reward,
            utility=utility,
            trusted=trusted,
        )
        self._records.append(evaluation)
        if len(self._records) > self.max_records:
            del self._records[: len(self._records) - self.max_records]
        return evaluation

    def rank_candidates(
        self, candidates: Sequence[CandidateAction]
    ) -> tuple[CandidateAction, ...]:
        """ANTE-TTO: rank by predicted utility, abstentions discounted to zero."""

        def score(candidate: CandidateAction) -> tuple[float, str]:
            utility = (
                float(candidate.expected_utility)
                if candidate.confidence_decision is ConfidenceDecision.TRUST
                else 0.0
            )
            return (-utility, candidate.candidate_id)

        return tuple(sorted(candidates, key=score))

    def prediction_schema(self) -> Mapping[str, object]:
        """JSON-schema-like fragment to splice into the worker action schema."""

        return {
            "predicted_exit_code": {"type": "integer"},
            "predicted_observation_digest": {"type": "string"},
            "confidence_decision": {
                "type": "string",
                "enum": [decision.value for decision in ConfidenceDecision],
            },
        }

    def summary(self) -> dict[str, object]:
        return {
            "target_accuracy": self.target_accuracy,
            "penalty_gamma": round(self.penalty_gamma, 6),
            "estimated_accuracy": round(self.estimate_accuracy(), 6),
            "records": len(self._records),
            "pending": len(self._pending),
        }


def bapo_advantage(
    base_advantage: float,
    prediction_reward: float,
    *,
    baseline_reward: float = 0.5,
    coefficient: float = 0.2,
) -> float:
    """Anchor a step-level prediction reward into the policy advantage.

    ``prediction_reward`` is a [0, 1] correctness score.  It is centred on the
    neutral baseline (0.5) and stretched to [-1, 1] before being scaled by the
    BAPO coefficient, so a perfect prediction adds ``+2 * coefficient`` while
    a complete miss subtracts the same amount.
    """

    if not 0.0 <= coefficient <= 1.0:
        raise ValueError("coefficient must be in [0, 1]")
    centered = 2.0 * (float(prediction_reward) - float(baseline_reward))
    return float(base_advantage) + coefficient * centered
