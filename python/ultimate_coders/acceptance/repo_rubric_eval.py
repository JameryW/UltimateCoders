"""Difficulty-aware loop planning and repository-grounded rubric scoring (ledger item #96).

Dory1.0-4B shows that a small looped model with a 256K context and only the last
loop's KV cache can beat much larger one-pass models while emitting 1.3x-2.5x
fewer tokens. The second half of this module scores a patch with a rubric whose
evidence must be grounded in the repository (changed paths, symbols, and test
names), so evaluation remains useful when execution is unavailable.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "REPO_RUBRIC_EVAL_ENV",
    "RepoRubricError",
    "Difficulty",
    "LoopPlan",
    "DoryLoopPlanner",
    "RubricDimension",
    "RubricItem",
    "RepoEvidence",
    "RubricResult",
    "RepositoryGroundedRubric",
    "RepoRubricReport",
    "RepoRubricEvaluator",
    "repo_rubric_eval_enabled",
]

REPO_RUBRIC_EVAL_ENV = "UC_REPO_RUBRIC_EVAL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def repo_rubric_eval_enabled() -> bool:
    return os.environ.get(REPO_RUBRIC_EVAL_ENV, "").strip().lower() in _TRUTHY


class RepoRubricError(ValueError):
    """Raised when a loop plan or rubric is malformed."""


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXTREME = "extreme"


@dataclass(frozen=True)
class LoopPlan:
    loops: int
    context_window: int
    kv_cache_multiplier: float
    output_token_factor: float

    def __post_init__(self) -> None:
        if self.loops < 1:
            raise RepoRubricError("loops must be positive")
        if self.context_window <= 0:
            raise RepoRubricError("context window must be positive")
        if self.kv_cache_multiplier <= 0:
            raise RepoRubricError("kv cache multiplier must be positive")
        if not 0.0 < self.output_token_factor <= 1.0:
            raise RepoRubricError("output token factor must be in (0, 1]")

    def effective_output_tokens(self, tokens: int) -> int:
        if tokens < 0:
            raise RepoRubricError("token count must not be negative")
        return max(1, int(tokens * self.output_token_factor))


class DoryLoopPlanner:
    """Deterministic approximation of Dory's difficulty-aware loop budget."""

    MAX_CONTEXT = 262_144

    _LOOPS = {
        Difficulty.EASY: 1,
        Difficulty.MEDIUM: 2,
        Difficulty.HARD: 4,
        Difficulty.EXTREME: 6,
    }

    def plan(self, difficulty: Difficulty, context_tokens: int | None = None) -> LoopPlan:
        loops = self._LOOPS.get(difficulty)
        if loops is None:
            raise RepoRubricError(f"unknown difficulty: {difficulty!r}")
        if context_tokens is not None:
            if context_tokens <= 0:
                raise RepoRubricError("context_tokens must be positive")
            if context_tokens > self.MAX_CONTEXT:
                raise RepoRubricError(f"context_tokens exceeds {self.MAX_CONTEXT}")
        # Only the last loop's KV is retained, so memory stays at one loop.
        factor = 1.0 if loops == 1 else max(0.4, 1.0 / (1.0 + 0.5 * (loops - 1)))
        return LoopPlan(
            loops=loops,
            context_window=self.MAX_CONTEXT,
            kv_cache_multiplier=1.0,
            output_token_factor=round(factor, 3),
        )


class RubricDimension(str, Enum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    TESTS = "tests"
    ARCHITECTURE = "architecture"
    MAINTAINABILITY = "maintainability"


@dataclass(frozen=True)
class RubricItem:
    item_id: str
    dimension: RubricDimension
    description: str
    weight: float = 1.0
    required_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.item_id.strip():
            raise RepoRubricError("item id must not be empty")
        if not self.description.strip():
            raise RepoRubricError("item description must not be empty")
        if self.weight <= 0:
            raise RepoRubricError("item weight must be positive")


@dataclass(frozen=True)
class RepoEvidence:
    changed_paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    test_names: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def terms(self) -> frozenset[str]:
        return frozenset(
            term.strip().lower()
            for term in (*self.changed_paths, *self.symbols, *self.test_names, *self.notes)
            if term.strip()
        )

    def coverage(self, required: tuple[str, ...]) -> tuple[float, tuple[str, ...]]:
        if not required:
            return 1.0, ()
        terms = self.terms()
        present = [term for term in required if term.strip().lower() in terms]
        missing = tuple(term for term in required if term.strip().lower() not in terms)
        return len(present) / len(required), missing


@dataclass(frozen=True)
class RubricResult:
    score: float
    passed: bool
    item_scores: tuple[tuple[str, float], ...]
    missing_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 6),
            "passed": self.passed,
            "item_scores": [
                {"item_id": item_id, "score": score} for item_id, score in self.item_scores
            ],
            "missing_evidence": list(self.missing_evidence),
        }


class RepositoryGroundedRubric:
    def __init__(self, items: tuple[RubricItem, ...], threshold: float = 0.7) -> None:
        if not items:
            raise RepoRubricError("at least one rubric item is required")
        if not 0.0 <= threshold <= 1.0:
            raise RepoRubricError("threshold must be in [0, 1]")
        self.items = items
        self.threshold = threshold

    def evaluate(self, evidence: RepoEvidence) -> RubricResult:
        total_weight = sum(item.weight for item in self.items)
        earned = 0.0
        item_scores: list[tuple[str, float]] = []
        missing: list[str] = []
        for item in self.items:
            ratio, absent = evidence.coverage(item.required_evidence)
            earned += item.weight * ratio
            item_scores.append((item.item_id, round(ratio, 6)))
            missing.extend(absent)
        score = earned / total_weight if total_weight else 0.0
        return RubricResult(
            score=round(score, 6),
            passed=score >= self.threshold,
            item_scores=tuple(item_scores),
            missing_evidence=tuple(dict.fromkeys(missing)),
        )


@dataclass(frozen=True)
class RepoRubricReport:
    difficulty: Difficulty
    plan: LoopPlan
    rubric: RubricResult
    patch_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "difficulty": self.difficulty.value,
            "loops": self.plan.loops,
            "context_window": self.plan.context_window,
            "output_token_factor": self.plan.output_token_factor,
            "rubric": self.rubric.to_dict(),
            "patch_digest": self.patch_digest,
        }


class RepoRubricEvaluator:
    """Runs the loop planner and repository-grounded rubric together."""

    def __init__(
        self,
        rubric: RepositoryGroundedRubric,
        planner: DoryLoopPlanner | None = None,
    ) -> None:
        self.rubric = rubric
        self.planner = planner or DoryLoopPlanner()

    def evaluate(
        self,
        patch: str,
        evidence: RepoEvidence,
        difficulty: Difficulty = Difficulty.HARD,
        context_tokens: int | None = None,
    ) -> RepoRubricReport:
        if not patch.strip():
            raise RepoRubricError("patch must not be empty")
        plan = self.planner.plan(difficulty, context_tokens)
        result = self.rubric.evaluate(evidence)
        digest = "sha256:" + hashlib.sha256(patch.encode("utf-8")).hexdigest()[:32]
        return RepoRubricReport(difficulty, plan, result, digest)
