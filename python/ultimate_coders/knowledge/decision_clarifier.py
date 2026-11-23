"""Decision-sensitive clarification and blocking governance obligations.

Ledger item #126 (arXiv:2611.13015 / 2611.03231). The clarifier asks only
about unknown parameters whose value would change the selected plan, ranked
by impact. The governance view rejects a specification that records an
enforced obligation without a concrete blocking action: traceability alone is
not enforcement.

Feature flag: ``UC_DECISION_CLARIFIER``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "ClarificationPlan",
    "ClarificationQuestion",
    "DECISION_CLARIFIER_ENV",
    "DecisionClarifier",
    "DecisionParameter",
    "GovernanceObligation",
    "GovernanceSpec",
    "SpecificationReport",
    "decision_clarifier_enabled",
]

DECISION_CLARIFIER_ENV = "UC_DECISION_CLARIFIER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def decision_clarifier_enabled() -> bool:
    return os.environ.get(DECISION_CLARIFIER_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class DecisionParameter:
    """A plan input and how much its unknown value matters."""

    name: str
    impact: float
    known: bool
    question: str
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if not 0.0 <= self.impact <= 1.0:
            raise ValueError("impact must be in [0, 1]")
        if not self.question.strip():
            raise ValueError("question must not be empty")


@dataclass(frozen=True)
class ClarificationQuestion:
    name: str
    impact: float
    question: str
    options: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "impact": self.impact,
            "question": self.question,
            "options": list(self.options),
        }


@dataclass(frozen=True)
class ClarificationPlan:
    questions: tuple[ClarificationQuestion, ...]
    complete: bool
    unresolved_impact: float
    risk: str

    def to_dict(self) -> dict[str, object]:
        return {
            "questions": [q.to_dict() for q in self.questions],
            "complete": self.complete,
            "unresolved_impact": round(self.unresolved_impact, 6),
            "risk": self.risk,
        }


class DecisionClarifier:
    """Ask the smallest set of questions that can change the plan."""

    def __init__(self, impact_threshold: float = 0.35, max_questions: int = 3) -> None:
        if not 0.0 <= impact_threshold <= 1.0:
            raise ValueError("impact_threshold must be in [0, 1]")
        if max_questions < 0:
            raise ValueError("max_questions must be non-negative")
        self.impact_threshold = impact_threshold
        self.max_questions = max_questions

    def plan(self, parameters: Iterable[DecisionParameter]) -> ClarificationPlan:
        params = list(parameters)
        unknown = [p for p in params if not p.known]
        unresolved = sum(p.impact for p in unknown)
        actionable = [p for p in unknown if p.impact >= self.impact_threshold]
        actionable.sort(key=lambda p: (-p.impact, p.name))
        chosen = actionable[: self.max_questions]
        questions = tuple(
            ClarificationQuestion(p.name, p.impact, p.question, p.options)
            for p in chosen
        )
        complete = not unknown
        if complete:
            risk = "none"
        elif chosen:
            risk = "ask"
        else:
            risk = "assume"
        return ClarificationPlan(questions, complete, unresolved, risk)

    def apply_answers(
        self,
        parameters: Iterable[DecisionParameter],
        answers: dict[str, bool],
    ) -> tuple[DecisionParameter, ...]:
        """Return parameters with any answered names marked known."""
        updated = []
        for parameter in parameters:
            known = parameter.name in answers if parameter.name in answers else parameter.known
            updated.append(
                DecisionParameter(
                    parameter.name,
                    parameter.impact,
                    bool(known),
                    parameter.question,
                    parameter.options,
                )
            )
        return tuple(updated)


@dataclass(frozen=True)
class GovernanceObligation:
    """A requirement plus the action that must block a non-compliant delivery."""

    obligation_id: str
    requirement: str
    blocking_action: str = ""
    enforced: bool = True

    def __post_init__(self) -> None:
        if not self.obligation_id.strip():
            raise ValueError("obligation_id must not be empty")
        if not self.requirement.strip():
            raise ValueError("requirement must not be empty")
        if self.enforced and not self.blocking_action.strip():
            raise ValueError("enforced obligations require a blocking action")


@dataclass(frozen=True)
class SpecificationReport:
    obligations: int
    enforced: int
    blocking: int
    compliant: bool
    missing_blocking: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "obligations": self.obligations,
            "enforced": self.enforced,
            "blocking": self.blocking,
            "compliant": self.compliant,
            "missing_blocking": list(self.missing_blocking),
        }


class GovernanceSpec:
    """Validate that governance rules can block, not merely be traced."""

    def __init__(self, obligations: Iterable[GovernanceObligation]) -> None:
        self.obligations = tuple(obligations)
        seen: set[str] = set()
        for obligation in self.obligations:
            if obligation.obligation_id in seen:
                raise ValueError(f"duplicate obligation {obligation.obligation_id}")
            seen.add(obligation.obligation_id)

    def report(self) -> SpecificationReport:
        enforced = [o for o in self.obligations if o.enforced]
        blocking = [o for o in enforced if o.blocking_action.strip()]
        missing = tuple(o.obligation_id for o in enforced if not o.blocking_action.strip())
        return SpecificationReport(
            obligations=len(self.obligations),
            enforced=len(enforced),
            blocking=len(blocking),
            compliant=not missing,
            missing_blocking=missing,
        )

    def enforce(self) -> SpecificationReport:
        report = self.report()
        if not report.compliant:
            raise ValueError(f"non-blocking enforced obligations: {report.missing_blocking}")
        return report
