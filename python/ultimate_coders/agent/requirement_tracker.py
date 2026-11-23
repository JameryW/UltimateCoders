"""Continuous requirement tracking and raw-failure replay (ledger item #89).

SWE-CoWork showed a large gap between F2P pass rate and complete delivery:
requirements need an explicit clarification-follow-up-verification state, and
a retry must receive the original failed trajectory rather than a model-written
lesson. This module is a small deterministic ledger for both behaviours.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "REQUIREMENT_TRACKER_ENV",
    "RequirementTrackerError",
    "RequirementStatus",
    "Requirement",
    "Clarification",
    "RequirementReport",
    "RequirementTracker",
    "FailureTrace",
    "RetryPlan",
    "build_retry_plan",
    "requirement_tracker_enabled",
]

REQUIREMENT_TRACKER_ENV = "UC_REQUIREMENT_TRACKER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def requirement_tracker_enabled() -> bool:
    return os.environ.get(REQUIREMENT_TRACKER_ENV, "").strip().lower() in _TRUTHY


class RequirementTrackerError(ValueError):
    """Raised when a requirement ledger operation is invalid."""


class RequirementStatus(str, Enum):
    OPEN = "open"
    CLARIFYING = "clarifying"
    FOLLOW_UP = "follow_up"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass
class Requirement:
    requirement_id: str
    text: str
    status: RequirementStatus = RequirementStatus.OPEN
    owner: str = ""
    evidence: str = ""
    clarifications: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.requirement_id.strip():
            raise RequirementTrackerError("requirement id must not be empty")
        if not self.text.strip():
            raise RequirementTrackerError("requirement text must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.requirement_id,
            "text": self.text,
            "status": self.status.value,
            "owner": self.owner,
            "evidence": self.evidence,
            "clarifications": list(self.clarifications),
        }


@dataclass(frozen=True)
class Clarification:
    clarification_id: str
    requirement_id: str
    question: str
    asked_turn: int
    answer: str = ""
    resolved: bool = False

    def __post_init__(self) -> None:
        if not self.clarification_id.strip():
            raise RequirementTrackerError("clarification id must not be empty")
        if not self.question.strip():
            raise RequirementTrackerError("question must not be empty")
        if self.asked_turn < 0:
            raise RequirementTrackerError("asked turn must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.clarification_id,
            "requirement": self.requirement_id,
            "question": self.question,
            "asked_turn": self.asked_turn,
            "answer": self.answer,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class RequirementReport:
    total: int
    by_status: tuple[tuple[str, int], ...]
    open_clarifications: int
    verified: int

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "by_status": dict(self.by_status),
            "open_clarifications": self.open_clarifications,
            "verified": self.verified,
        }


@dataclass
class RequirementTracker:
    _requirements: dict[str, Requirement] = field(default_factory=dict)
    _clarifications: dict[str, Clarification] = field(default_factory=dict)
    _next_clarification: int = 1

    def add(
        self,
        requirement_id: str,
        text: str,
        *,
        owner: str = "",
    ) -> Requirement:
        if requirement_id in self._requirements:
            raise RequirementTrackerError(f"duplicate requirement: {requirement_id}")
        requirement = Requirement(requirement_id, text, owner=owner)
        self._requirements[requirement_id] = requirement
        return requirement

    def ask(self, requirement_id: str, question: str, *, turn: int) -> Clarification:
        requirement = self._requirements.get(requirement_id)
        if requirement is None:
            raise RequirementTrackerError(f"unknown requirement: {requirement_id}")
        clarification = Clarification(
            clarification_id=f"clarification-{self._next_clarification}",
            requirement_id=requirement_id,
            question=question,
            asked_turn=turn,
        )
        self._next_clarification += 1
        self._clarifications[clarification.clarification_id] = clarification
        requirement.clarifications.append(clarification.clarification_id)
        requirement.status = RequirementStatus.CLARIFYING
        return clarification

    def answer(self, clarification_id: str, answer: str) -> Clarification:
        clarification = self._clarifications.get(clarification_id)
        if clarification is None:
            raise RequirementTrackerError(f"unknown clarification: {clarification_id}")
        if not answer.strip():
            raise RequirementTrackerError("answer must not be empty")
        resolved = Clarification(
            clarification.clarification_id,
            clarification.requirement_id,
            clarification.question,
            clarification.asked_turn,
            answer=answer,
            resolved=True,
        )
        self._clarifications[clarification_id] = resolved
        requirement = self._requirements[clarification.requirement_id]
        if all(self._clarifications[item].resolved for item in requirement.clarifications):
            requirement.status = RequirementStatus.FOLLOW_UP
        return resolved

    def verify(self, requirement_id: str, evidence: str) -> Requirement:
        requirement = self._requirements.get(requirement_id)
        if requirement is None:
            raise RequirementTrackerError(f"unknown requirement: {requirement_id}")
        if not evidence.strip():
            raise RequirementTrackerError("verification evidence must not be empty")
        if any(not self._clarifications[item].resolved for item in requirement.clarifications):
            raise RequirementTrackerError("cannot verify with open clarifications")
        requirement.status = RequirementStatus.VERIFIED
        requirement.evidence = evidence
        return requirement

    def fail(self, requirement_id: str) -> Requirement:
        requirement = self._requirements.get(requirement_id)
        if requirement is None:
            raise RequirementTrackerError(f"unknown requirement: {requirement_id}")
        requirement.status = RequirementStatus.FAILED
        return requirement

    def report(self) -> RequirementReport:
        counts: dict[str, int] = {}
        for requirement in self._requirements.values():
            counts[requirement.status.value] = counts.get(requirement.status.value, 0) + 1
        open_clarifications = sum(
            1 for item in self._clarifications.values() if not item.resolved
        )
        return RequirementReport(
            len(self._requirements),
            tuple(sorted(counts.items())),
            open_clarifications,
            counts.get(RequirementStatus.VERIFIED.value, 0),
        )

    def pending_questions(self) -> tuple[Clarification, ...]:
        return tuple(
            item for item in self._clarifications.values() if not item.resolved
        )


@dataclass(frozen=True)
class FailureTrace:
    attempt_id: str
    raw_trajectory: str
    tests_passed: int
    tests_total: int
    f2p_passed: bool = False

    def __post_init__(self) -> None:
        if not self.attempt_id.strip():
            raise RequirementTrackerError("attempt id must not be empty")
        if not self.raw_trajectory.strip():
            raise RequirementTrackerError("raw trajectory must not be empty")
        if self.tests_total < 0 or self.tests_passed < 0:
            raise RequirementTrackerError("test counts must be non-negative")
        if self.tests_passed > self.tests_total:
            raise RequirementTrackerError("passed tests cannot exceed total")


@dataclass(frozen=True)
class RetryPlan:
    attempt_id: str
    raw_trajectory: str
    run_full_regression: bool = True
    do_not_summarize: bool = True
    reason: str = "raw failed trajectory preserves search and edit evidence"

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt_id,
            "raw_trajectory": self.raw_trajectory,
            "run_full_regression": self.run_full_regression,
            "do_not_summarize": self.do_not_summarize,
            "reason": self.reason,
        }


def build_retry_plan(
    failure: FailureTrace,
    *,
    run_full_regression: bool = True,
) -> RetryPlan:
    if not isinstance(failure, FailureTrace):
        raise RequirementTrackerError("failure must be a FailureTrace")
    if not run_full_regression:
        raise RequirementTrackerError("a retry must always run the full regression suite")
    return RetryPlan(
        attempt_id=failure.attempt_id,
        raw_trajectory=failure.raw_trajectory,
        run_full_regression=True,
        do_not_summarize=True,
    )
