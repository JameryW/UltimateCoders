"""Contextual Early Reward (CER): prefix-only terminal reward prediction (#34).

Ledger item #34 adapts CER (arXiv:2609.31995).  Waiting for a full long-horizon
rollout before learning whether an attempt is viable wastes most of the
policy and judge tokens.  CER retrieves related historical tasks, synthesises an
adaptive stage rubric, and predicts the terminal reward from behaviour evidence
already present in the trajectory prefix.

The prediction is deliberately conservative: it never terminates an attempt
that has passed a test, and it only prunes at caller-chosen milestones.

Feature flag: ``UC_CER_PREDICTOR`` (default off).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "UC_CER_PREDICTOR",
    "Stage",
    "HistoricalTask",
    "RubricItem",
    "AdaptiveRubric",
    "PrefixFeatures",
    "CerPrediction",
    "CerPredictor",
    "cer_predictor_enabled",
    "predict_reward",
]

UC_CER_PREDICTOR = "UC_CER_PREDICTOR"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def cer_predictor_enabled() -> bool:
    """Return whether prefix-only terminal reward prediction is enabled."""

    return os.environ.get(UC_CER_PREDICTOR, "").strip().lower() in _TRUTHY


class Stage(str, Enum):
    LOCALIZATION = "localization"
    EDITING = "editing"
    VERIFICATION = "verification"
    REPAIR = "repair"


_STAGE_WEIGHTS = {
    Stage.LOCALIZATION: 0.25,
    Stage.EDITING: 0.30,
    Stage.VERIFICATION: 0.35,
    Stage.REPAIR: 0.10,
}


@dataclass(frozen=True)
class HistoricalTask:
    """One related task used to adapt the rubric and threshold."""

    task_id: str
    tags: tuple[str, ...] = ()
    resolved: bool = True
    stage_rewards: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", str(self.task_id).strip())
        object.__setattr__(self, "tags", tuple(str(tag).strip().lower() for tag in self.tags))
        object.__setattr__(
            self,
            "stage_rewards",
            {str(key): float(value) for key, value in self.stage_rewards.items()},
        )


@dataclass(frozen=True)
class RubricItem:
    name: str
    weight: float
    stage: Stage
    evidence: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "weight": round(self.weight, 6),
            "stage": self.stage.value,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class AdaptiveRubric:
    task_tags: tuple[str, ...]
    milestone: int
    items: tuple[RubricItem, ...]
    prune_threshold: float
    relevance: float

    def weight_of(self, stage: Stage) -> float:
        return sum(item.weight for item in self.items if item.stage is stage)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_tags": list(self.task_tags),
            "milestone": self.milestone,
            "items": [item.to_dict() for item in self.items],
            "prune_threshold": round(self.prune_threshold, 6),
            "relevance": round(self.relevance, 6),
        }


@dataclass(frozen=True)
class PrefixFeatures:
    """Behaviour evidence available before the terminal result exists."""

    step: int
    stage: Stage = Stage.LOCALIZATION
    tests_passed: int = 0
    tests_failed: int = 0
    required_files_touched: int = 0
    required_files_total: int = 0
    verification_seen: bool = False
    unresolved_errors: int = 0
    progress_ratio: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "step", max(0, int(self.step)))
        object.__setattr__(self, "tests_passed", max(0, int(self.tests_passed)))
        object.__setattr__(self, "tests_failed", max(0, int(self.tests_failed)))
        object.__setattr__(self, "required_files_touched", max(0, int(self.required_files_touched)))
        object.__setattr__(self, "required_files_total", max(0, int(self.required_files_total)))
        object.__setattr__(self, "unresolved_errors", max(0, int(self.unresolved_errors)))
        object.__setattr__(self, "progress_ratio", min(1.0, max(0.0, float(self.progress_ratio))))

    @property
    def tests_total(self) -> int:
        return self.tests_passed + self.tests_failed

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "stage": self.stage.value,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "required_files_touched": self.required_files_touched,
            "required_files_total": self.required_files_total,
            "verification_seen": self.verification_seen,
            "unresolved_errors": self.unresolved_errors,
            "progress_ratio": round(self.progress_ratio, 6),
        }


@dataclass(frozen=True)
class CerPrediction:
    reward: float
    confidence: float
    rubric: AdaptiveRubric
    prune: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reward": round(self.reward, 6),
            "confidence": round(self.confidence, 6),
            "rubric": self.rubric.to_dict(),
            "prune": self.prune,
            "reason": self.reason,
        }


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = {value.lower() for value in left}
    right_set = {value.lower() for value in right}
    union = left_set | right_set
    if not union:
        return 0.0
    return len(left_set & right_set) / len(union)


class CerPredictor:
    """Adaptive stage rubric plus conservative prefix reward prediction."""

    def __init__(
        self,
        history: Sequence[HistoricalTask] = (),
        *,
        base_threshold: float = 0.35,
    ) -> None:
        self.history = tuple(history)
        self.base_threshold = min(1.0, max(0.0, float(base_threshold)))

    def _relevance(self, task_tags: Sequence[str]) -> float:
        if not self.history:
            return 0.0
        scores = [_jaccard(task_tags, task.tags) for task in self.history]
        return sum(scores) / len(scores)

    def build_rubric(self, task_tags: Sequence[str], *, milestone: int = 10) -> AdaptiveRubric:
        relevance = self._relevance(task_tags)
        tags = tuple(str(tag).strip().lower() for tag in task_tags)
        items: list[RubricItem] = []
        for stage, base_weight in _STAGE_WEIGHTS.items():
            weight = base_weight * (0.75 + 0.5 * relevance)
            items.append(
                RubricItem(
                    name=f"{stage.value}_evidence",
                    weight=weight,
                    stage=stage,
                    evidence=self._evidence_hint(stage),
                )
            )
        total = sum(item.weight for item in items)
        normalised = tuple(
            RubricItem(
                name=item.name,
                weight=item.weight / total if total else 0.0,
                stage=item.stage,
                evidence=item.evidence,
            )
            for item in items
        )
        threshold = min(0.9, max(0.05, self.base_threshold * (1.0 - 0.25 * relevance)))
        return AdaptiveRubric(
            task_tags=tags,
            milestone=max(1, int(milestone)),
            items=normalised,
            prune_threshold=threshold,
            relevance=round(relevance, 6),
        )

    @staticmethod
    def _evidence_hint(stage: Stage) -> str:
        hints = {
            Stage.LOCALIZATION: "required files touched and cross-module reads",
            Stage.EDITING: "progress ratio and a substantive patch",
            Stage.VERIFICATION: "tests run after the final edit",
            Stage.REPAIR: "unresolved errors resolved rather than hidden",
        }
        return hints[stage]

    @staticmethod
    def _stage_score(stage: Stage, features: PrefixFeatures) -> float:
        if stage is Stage.LOCALIZATION:
            if features.required_files_total:
                coverage = features.required_files_touched / features.required_files_total
            else:
                coverage = features.progress_ratio
            return min(1.0, 0.7 * coverage + 0.3 * min(1.0, features.step / 10.0))
        if stage is Stage.EDITING:
            touched = 0.6 if features.required_files_touched else 0.0
            return min(1.0, 0.4 * features.progress_ratio + touched)
        if stage is Stage.VERIFICATION:
            if features.tests_total == 0:
                return 0.0
            pass_ratio = features.tests_passed / features.tests_total
            return min(1.0, 0.7 * pass_ratio + (0.3 if features.verification_seen else 0.0))
        penalty = min(1.0, 0.2 * features.unresolved_errors)
        return max(0.0, 1.0 - penalty)

    def predict(
        self,
        features: PrefixFeatures,
        task_tags: Sequence[str] = (),
        *,
        milestone: int = 10,
    ) -> CerPrediction:
        """Predict terminal reward from the current prefix and decide on pruning."""

        rubric = self.build_rubric(task_tags, milestone=milestone)
        reward = sum(item.weight * self._stage_score(item.stage, features) for item in rubric.items)
        history_factor = min(1.0, 0.45 + 0.1 * len(self.history))
        step_factor = min(1.0, 0.5 + features.step / max(1, milestone * 2))
        confidence = min(1.0, history_factor * (0.7 + 0.3 * step_factor))

        prune = (
            features.step >= rubric.milestone
            and features.tests_passed == 0
            and reward < rubric.prune_threshold
            and features.progress_ratio < 0.5
        )
        if prune:
            reason = (
                f"prefix reward {reward:.3f} is below the adaptive threshold "
                f"{rubric.prune_threshold:.3f} with no passing test"
            )
        elif features.step < rubric.milestone:
            reason = "milestone not reached; predicting without pruning"
        else:
            reason = "prefix evidence does not justify early termination"
        return CerPrediction(
            reward=round(reward, 6),
            confidence=round(confidence, 6),
            rubric=rubric,
            prune=prune,
            reason=reason,
        )

    def milestone_scores(
        self,
        features: PrefixFeatures,
        task_tags: Sequence[str] = (),
        *,
        milestones: Sequence[int] = (10, 20),
    ) -> tuple[CerPrediction, ...]:
        """Score the same prefix at several caller-chosen milestones."""

        return tuple(
            self.predict(features, task_tags, milestone=milestone) for milestone in milestones
        )


def predict_reward(
    features: PrefixFeatures,
    history: Sequence[HistoricalTask] = (),
    task_tags: Sequence[str] = (),
    *,
    milestone: int = 10,
) -> CerPrediction:
    """Convenience wrapper for a one-shot CER prediction."""

    return CerPredictor(history).predict(features, task_tags, milestone=milestone)
