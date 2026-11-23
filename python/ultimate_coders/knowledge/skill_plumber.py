"""Instruction-level skill coverage and self-evolution (V28 item #127).

The ledger's SkillPlumber work separates two failure modes that most skill
evolution loops conflate: an instruction whose wording is wrong, and an
instruction the executor simply did not follow.  This module implements the
deterministic bookkeeping half of that loop:

* classify every skill instruction as ``FOLLOW``, ``NOT_FOLLOW`` or
  ``NOT_COVERED`` after a run;
* turn the coverage matrix into revisable questions;
* validate a question against the next round's observation; and
* distill only validated conclusions into a shared policy bank.

Nothing here calls a model.  A caller supplies the observation labels, and
the module keeps the accounting bounded and reproducible.  ``UC_SKILL_PLUMBER``
is the opt-in flag; the default is off.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "SKILL_PLUMBER_ENV",
    "InstructionStatus",
    "QuestionKind",
    "Instruction",
    "Observation",
    "InstructionReport",
    "CoverageMatrix",
    "RevisionQuestion",
    "PolicyEntry",
    "PolicyBank",
    "MaintainerAgent",
    "SkillPlumber",
    "skill_plumber_enabled",
]

SKILL_PLUMBER_ENV = "UC_SKILL_PLUMBER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def skill_plumber_enabled() -> bool:
    """Return whether the instruction-coverage loop is enabled."""

    return os.environ.get(SKILL_PLUMBER_ENV, "").strip().lower() in _TRUTHY


class InstructionStatus(str, Enum):
    """Three-state label for one instruction after a task round."""

    FOLLOW = "follow"
    NOT_FOLLOW = "not-follow"
    NOT_COVERED = "not-covered"


class QuestionKind(str, Enum):
    """How a revision question should be resolved."""

    REVISE_WORDING = "revise-wording"
    REINFORCE_EXECUTION = "reinforce-execution"
    ADD_COVERAGE = "add-coverage"


@dataclass(frozen=True)
class Instruction:
    """One instruction in a skill."""

    id: str
    text: str
    critical: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("instruction id must not be empty")
        if not self.text.strip():
            raise ValueError("instruction text must not be empty")


@dataclass(frozen=True)
class Observation:
    """One executor observation for an instruction."""

    instruction_id: str
    status: InstructionStatus
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.instruction_id.strip():
            raise ValueError("instruction_id must not be empty")
        if not isinstance(self.status, InstructionStatus):
            raise ValueError("status must be an InstructionStatus")


@dataclass(frozen=True)
class InstructionReport:
    """Aggregated labels for one instruction across a run."""

    instruction_id: str
    follow: int = 0
    not_follow: int = 0
    not_covered: int = 0

    @property
    def total(self) -> int:
        return self.follow + self.not_follow + self.not_covered

    @property
    def status(self) -> InstructionStatus:
        if self.not_follow:
            return InstructionStatus.NOT_FOLLOW
        if self.not_covered and not self.follow:
            return InstructionStatus.NOT_COVERED
        if self.follow:
            return InstructionStatus.FOLLOW
        return InstructionStatus.NOT_COVERED

    def to_dict(self) -> dict[str, object]:
        return {
            "instruction_id": self.instruction_id,
            "follow": self.follow,
            "not_follow": self.not_follow,
            "not_covered": self.not_covered,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class CoverageMatrix:
    """Coverage of a skill's instructions for one round."""

    skill_id: str
    reports: tuple[InstructionReport, ...]
    observations: int

    @property
    def coverage(self) -> float:
        if not self.reports:
            return 0.0
        covered = sum(1 for r in self.reports if r.total > 0)
        return covered / len(self.reports)

    @property
    def follow_rate(self) -> float:
        observed = sum(r.follow + r.not_follow for r in self.reports)
        if not observed:
            return 0.0
        followed = sum(r.follow for r in self.reports)
        return followed / observed

    def by_instruction(self, instruction_id: str) -> InstructionReport | None:
        for report in self.reports:
            if report.instruction_id == instruction_id:
                return report
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "coverage": round(self.coverage, 4),
            "follow_rate": round(self.follow_rate, 4),
            "reports": [r.to_dict() for r in self.reports],
        }


@dataclass(frozen=True)
class RevisionQuestion:
    """A concrete question to verify in the next round."""

    id: str
    instruction_id: str
    kind: QuestionKind
    text: str
    critical: bool = False
    validated: bool = False
    evidence: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "instruction_id": self.instruction_id,
            "kind": self.kind.value,
            "text": self.text,
            "critical": self.critical,
            "validated": self.validated,
        }


@dataclass(frozen=True)
class PolicyEntry:
    """A validated conclusion distilled into the shared policy bank."""

    id: str
    instruction_id: str
    kind: QuestionKind
    text: str


class PolicyBank:
    """Bounded, append-only bank of validated skill policies."""

    def __init__(self, max_entries: int = 256) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: list[PolicyEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> tuple[PolicyEntry, ...]:
        return tuple(self._entries)

    def add(self, entry: PolicyEntry) -> bool:
        if any(e.id == entry.id for e in self._entries):
            return False
        if len(self._entries) >= self._max_entries:
            self._entries.pop(0)
        self._entries.append(entry)
        return True


class MaintainerAgent:
    """Deterministic distillation of validated questions into policies."""

    def distill(
        self,
        questions: Iterable[RevisionQuestion],
        results: Mapping[str, bool],
    ) -> tuple[PolicyEntry, ...]:
        entries: list[PolicyEntry] = []
        seen: set[str] = set()
        for question in questions:
            if not results.get(question.id, False):
                continue
            if question.id in seen:
                continue
            seen.add(question.id)
            entries.append(
                PolicyEntry(
                    id=f"policy-{question.id}",
                    instruction_id=question.instruction_id,
                    kind=question.kind,
                    text=(
                        f"{question.kind.value}: {question.text} "
                        "(validated by a later round)"
                    ),
                )
            )
        return tuple(entries)


class SkillPlumber:
    """Analyse instruction coverage, ask revision questions, distill policies."""

    def __init__(self, max_instructions: int = 512, bank: PolicyBank | None = None) -> None:
        if max_instructions <= 0:
            raise ValueError("max_instructions must be positive")
        self._max_instructions = max_instructions
        self._bank = bank if bank is not None else PolicyBank()

    @property
    def bank(self) -> PolicyBank:
        return self._bank

    def analyze(
        self,
        skill_id: str,
        instructions: Iterable[Instruction],
        observations: Iterable[Observation],
    ) -> CoverageMatrix:
        skill = skill_id.strip()
        if not skill:
            raise ValueError("skill_id must not be empty")
        ordered = tuple(instructions)
        if not ordered:
            raise ValueError("at least one instruction is required")
        if len(ordered) > self._max_instructions:
            raise ValueError("instruction capacity exceeded")
        ids = [i.id for i in ordered]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate instruction id")

        counts: dict[str, list[int]] = {i.id: [0, 0, 0] for i in ordered}
        seen_observations = 0
        for observation in observations:
            slots = counts.get(observation.instruction_id)
            if slots is None:
                raise ValueError(f"unknown instruction {observation.instruction_id!r}")
            if observation.status is InstructionStatus.FOLLOW:
                slots[0] += 1
            elif observation.status is InstructionStatus.NOT_FOLLOW:
                slots[1] += 1
            else:
                slots[2] += 1
            seen_observations += 1

        reports = tuple(
            InstructionReport(
                instruction_id=i.id,
                follow=counts[i.id][0],
                not_follow=counts[i.id][1],
                not_covered=counts[i.id][2],
            )
            for i in ordered
        )
        matrix = CoverageMatrix(skill, reports, seen_observations)
        return matrix

    def questions(
        self,
        matrix: CoverageMatrix,
        instructions: Iterable[Instruction] | None = None,
    ) -> tuple[RevisionQuestion, ...]:
        critical = {i.id: i.critical for i in (instructions or ())}
        questions: list[RevisionQuestion] = []
        for report in matrix.reports:
            if report.not_follow:
                kind = QuestionKind.REINFORCE_EXECUTION
                text = (
                    f"Why was instruction {report.instruction_id} not followed? "
                    "Add an executable tool constraint or an explicit verification step."
                )
            elif report.not_covered and not report.follow:
                kind = QuestionKind.ADD_COVERAGE
                text = (
                    f"Instruction {report.instruction_id} was never exercised; "
                    "add a task fixture that covers it."
                )
            elif report.follow and report.not_covered:
                kind = QuestionKind.ADD_COVERAGE
                text = (
                    f"Instruction {report.instruction_id} has partial coverage; "
                    "extend the fixture to cover the remaining branch."
                )
            else:
                continue
            questions.append(
                RevisionQuestion(
                    id=f"q-{matrix.skill_id}-{report.instruction_id}",
                    instruction_id=report.instruction_id,
                    kind=kind,
                    text=text,
                    critical=critical.get(report.instruction_id, False),
                )
            )
        return tuple(questions)

    def validate(
        self,
        question: RevisionQuestion,
        observed: InstructionStatus,
        evidence: str = "",
    ) -> RevisionQuestion:
        if not isinstance(observed, InstructionStatus):
            raise ValueError("observed must be an InstructionStatus")
        validated = observed is InstructionStatus.FOLLOW
        return RevisionQuestion(
            id=question.id,
            instruction_id=question.instruction_id,
            kind=question.kind,
            text=question.text,
            critical=question.critical,
            validated=validated,
            evidence=evidence,
        )

    def distill(
        self,
        questions: Iterable[RevisionQuestion],
        results: Mapping[str, bool],
    ) -> tuple[PolicyEntry, ...]:
        entries = MaintainerAgent().distill(questions, results)
        for entry in entries:
            self._bank.add(entry)
        return entries
