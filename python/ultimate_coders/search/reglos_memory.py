"""Append-only personal memory and cross-family critic loop (ledger item #93).

Reglos keeps an append-only record of user preferences: a newer preference
overrides an older one without deleting history, and a forget op appends a
tombstone. Harness patches proposed by a coding agent must pass a critic from
a second model family before a frozen evaluator accepts them. This module
models the ledger and the gates; it does not call a model or a benchmark.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "REGLOS_MEMORY_ENV",
    "ReglosMemoryError",
    "MemoryKind",
    "MemoryRecord",
    "AppendOnlyMemory",
    "HarnessProposal",
    "CriticReview",
    "CrossFamilyCritic",
    "FrozenEvaluator",
    "ProposalDecision",
    "ReglosLoop",
    "reglos_memory_enabled",
]

REGLOS_MEMORY_ENV = "UC_REGLOS_MEMORY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def reglos_memory_enabled() -> bool:
    return os.environ.get(REGLOS_MEMORY_ENV, "").strip().lower() in _TRUTHY


class ReglosMemoryError(ValueError):
    """Raised when a memory record or proposal is malformed."""


class MemoryKind(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    TOMBSTONE = "tombstone"


@dataclass(frozen=True)
class MemoryRecord:
    record_id: int
    key: str
    value: str
    kind: MemoryKind
    superseded: bool = False

    def __post_init__(self) -> None:
        if self.record_id <= 0:
            raise ReglosMemoryError("record id must be positive")
        if not self.key.strip():
            raise ReglosMemoryError("memory key must not be empty")
        if self.kind is not MemoryKind.TOMBSTONE and not self.value.strip():
            raise ReglosMemoryError("memory value must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "key": self.key,
            "value": self.value,
            "kind": self.kind.value,
            "superseded": self.superseded,
        }


class AppendOnlyMemory:
    """Append-only ledger with active-view projection."""

    def __init__(self) -> None:
        self._records: list[MemoryRecord] = []

    def append(
        self, key: str, value: str, kind: MemoryKind = MemoryKind.PREFERENCE
    ) -> MemoryRecord:
        if kind is MemoryKind.TOMBSTONE:
            raise ReglosMemoryError("use forget() to append a tombstone")
        record = MemoryRecord(len(self._records) + 1, key, value, kind)
        self._records.append(record)
        return record

    def forget(self, key: str) -> MemoryRecord:
        record = MemoryRecord(len(self._records) + 1, key, "", MemoryKind.TOMBSTONE)
        self._records.append(record)
        return record

    def active(self, key: str) -> MemoryRecord | None:
        for record in reversed(self._records):
            if record.key != key:
                continue
            if record.kind is MemoryKind.TOMBSTONE:
                return None
            return record
        return None

    def snapshot(self) -> tuple[MemoryRecord, ...]:
        latest: dict[str, MemoryRecord] = {}
        for record in self._records:
            if record.kind is MemoryKind.TOMBSTONE:
                latest.pop(record.key, None)
            else:
                latest[record.key] = record
        return tuple(latest[key] for key in sorted(latest))

    def history(self, key: str | None = None) -> tuple[MemoryRecord, ...]:
        if key is None:
            return tuple(self._records)
        return tuple(record for record in self._records if record.key == key)

    def __len__(self) -> int:
        return len(self._records)


@dataclass(frozen=True)
class HarnessProposal:
    proposal_id: str
    author_family: str
    patch: str
    claimed_score: float = 0.0
    gaming_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.proposal_id.strip():
            raise ReglosMemoryError("proposal id must not be empty")
        if not self.author_family.strip():
            raise ReglosMemoryError("author family must not be empty")
        if not self.patch.strip():
            raise ReglosMemoryError("proposal patch must not be empty")
        if not 0.0 <= self.claimed_score <= 1.0:
            raise ReglosMemoryError("claimed score must be in [0, 1]")


@dataclass(frozen=True)
class CriticReview:
    critic_family: str
    approved: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "critic_family": self.critic_family,
            "approved": self.approved,
            "reasons": list(self.reasons),
        }


class CrossFamilyCritic:
    """Rejects same-family reviews and proposals with gaming indicators."""

    def review(self, proposal: HarnessProposal, critic_family: str) -> CriticReview:
        if not critic_family.strip():
            raise ReglosMemoryError("critic family must not be empty")
        if critic_family.strip().lower() == proposal.author_family.strip().lower():
            return CriticReview(
                critic_family, False, ("critic must be from a different model family",)
            )
        if proposal.gaming_flags:
            return CriticReview(
                critic_family,
                False,
                tuple(f"benchmark gaming: {flag}" for flag in proposal.gaming_flags),
            )
        return CriticReview(critic_family, True, ())


@dataclass(frozen=True)
class EvaluationResult:
    proposal_id: str
    measured_score: float
    accepted: bool

    def __post_init__(self) -> None:
        if not self.proposal_id.strip():
            raise ReglosMemoryError("proposal id must not be empty")
        if not 0.0 <= self.measured_score <= 1.0:
            raise ReglosMemoryError("measured score must be in [0, 1]")


class FrozenEvaluator:
    def __init__(self, baseline_score: float) -> None:
        if not 0.0 <= baseline_score <= 1.0:
            raise ReglosMemoryError("baseline score must be in [0, 1]")
        self.baseline_score = baseline_score

    def evaluate(self, proposal: HarnessProposal, measured_score: float) -> EvaluationResult:
        if not 0.0 <= measured_score <= 1.0:
            raise ReglosMemoryError("measured score must be in [0, 1]")
        return EvaluationResult(
            proposal.proposal_id,
            measured_score,
            measured_score >= self.baseline_score,
        )


@dataclass(frozen=True)
class ProposalDecision:
    proposal_id: str
    accepted: bool
    critic: CriticReview
    evaluation: EvaluationResult

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "accepted": self.accepted,
            "critic": self.critic.to_dict(),
            "evaluation": {
                "proposal_id": self.evaluation.proposal_id,
                "measured_score": self.evaluation.measured_score,
                "accepted": self.evaluation.accepted,
            },
        }


class ReglosLoop:
    """Runs the critic-then-frozen-evaluator gate and records accepted lessons."""

    def __init__(
        self,
        memory: AppendOnlyMemory,
        critic: CrossFamilyCritic,
        evaluator: FrozenEvaluator,
    ) -> None:
        self.memory = memory
        self.critic = critic
        self.evaluator = evaluator

    def evaluate_proposal(
        self,
        proposal: HarnessProposal,
        critic_family: str,
        measured_score: float,
    ) -> ProposalDecision:
        review = self.critic.review(proposal, critic_family)
        evaluation = self.evaluator.evaluate(proposal, measured_score)
        accepted = review.approved and evaluation.accepted
        if accepted:
            self.memory.append(
                f"harness:{proposal.proposal_id}",
                f"{proposal.patch.strip()} (score={measured_score:.3f})",
                MemoryKind.FACT,
            )
        return ProposalDecision(proposal.proposal_id, accepted, review, evaluation)
