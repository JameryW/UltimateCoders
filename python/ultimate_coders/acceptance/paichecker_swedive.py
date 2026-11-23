"""PAIChecker PR/Issue cleaning and SWE-DIVE multi-turn user simulation.

Implements ledger item #65 (PAIChecker, arXiv:2607.28587 and SWE-DIVE,
arXiv:2610.30047). The cleaner rejects training tasks whose pull request does
not implement the issue it claims to solve. The simulator exercises an agent
with a mid-session requirement change and a strict dead-code review.

This module is deterministic. It does not call GitHub, parse a real AST, or run
an agent; callers supply the task record and the agent responses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CleaningDecision",
    "CleaningReport",
    "DynamicUserSimulator",
    "MisalignmentFinding",
    "MisalignmentPattern",
    "PAIChecker",
    "PAICHECKER_SWEDIVE_ENV",
    "SessionReport",
    "SessionValidation",
    "SimulatorRole",
    "TaskRecord",
    "UserTurn",
    "paichecker_swedive_enabled",
]

PAICHECKER_SWEDIVE_ENV = "UC_PAICHECKER_SWEDIVE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def paichecker_swedive_enabled() -> bool:
    return os.environ.get(PAICHECKER_SWEDIVE_ENV, "").strip().lower() in _TRUTHY


class PaicheckerError(ValueError):
    """Malformed task or session evidence."""


class MisalignmentPattern(str, Enum):
    UNRELATED_MODULE = "unrelated_module"
    UNRELATED_TESTS = "unrelated_tests"
    PATCH_SCOPE_MISMATCH = "patch_scope_mismatch"
    UNDER_SPECIFIED_ISSUE = "under_specified_issue"
    TEST_ONLY_CHANGE = "test_only_change"


@dataclass(frozen=True)
class TaskRecord:
    instance_id: str
    issue: str
    patch_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    changed_symbols: tuple[str, ...] = ()
    issue_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise PaicheckerError("instance_id must not be empty")
        if not self.issue.strip():
            raise PaicheckerError("issue must not be empty")
        if not self.patch_paths:
            raise PaicheckerError("patch_paths must not be empty")
        if len(set(self.patch_paths)) != len(self.patch_paths):
            raise PaicheckerError("patch paths must be unique")

    @property
    def test_only(self) -> bool:
        return all(
            path.startswith("tests/") or "/test" in path or path.startswith("test")
            for path in self.patch_paths
        )

    def haystack(self) -> str:
        return " ".join((self.issue, *self.issue_terms)).lower()


@dataclass(frozen=True)
class MisalignmentFinding:
    pattern: MisalignmentPattern
    confidence: float
    evidence: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise PaicheckerError("confidence must be in [0, 1]")

    def to_dict(self) -> dict[str, object]:
        return {
            "pattern": self.pattern.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class CleaningDecision:
    accepted: bool
    findings: tuple[MisalignmentFinding, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "findings": [finding.to_dict() for finding in self.findings],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CleaningReport:
    decisions: tuple[CleaningDecision, ...]

    @property
    def accepted(self) -> int:
        return sum(1 for decision in self.decisions if decision.accepted)

    @property
    def rejected(self) -> int:
        return len(self.decisions) - self.accepted


class PAIChecker:
    """Three-stage cleaner: patterns, cross-agent labels, code verification."""

    PATTERN_ORDER = tuple(MisalignmentPattern)

    def detect(self, task: TaskRecord) -> tuple[MisalignmentFinding, ...]:
        haystack = task.haystack()
        findings: list[MisalignmentFinding] = []

        for path in task.patch_paths:
            if not self._path_mentioned(path, haystack):
                findings.append(
                    MisalignmentFinding(
                        MisalignmentPattern.UNRELATED_MODULE,
                        0.8,
                        f"patch path not mentioned by the issue: {path}",
                    )
                )
                break

        for path in task.test_paths:
            if not self._path_mentioned(path, haystack):
                findings.append(
                    MisalignmentFinding(
                        MisalignmentPattern.UNRELATED_TESTS,
                        0.72,
                        f"test path not mentioned by the issue: {path}",
                    )
                )
                break

        if task.changed_symbols and not any(
            symbol.lower() in haystack for symbol in task.changed_symbols
        ):
            findings.append(
                MisalignmentFinding(
                    MisalignmentPattern.PATCH_SCOPE_MISMATCH,
                    0.76,
                    "none of the changed symbols appears in the issue",
                )
            )

        if len(task.issue.strip()) < 40:
            findings.append(
                MisalignmentFinding(
                    MisalignmentPattern.UNDER_SPECIFIED_ISSUE,
                    0.65,
                    "issue is shorter than 40 characters",
                )
            )

        if task.test_only:
            findings.append(
                MisalignmentFinding(
                    MisalignmentPattern.TEST_ONLY_CHANGE,
                    0.9,
                    "every patched path looks like a test path",
                )
            )

        return tuple(findings)

    @staticmethod
    def _path_mentioned(path: str, haystack: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        if normalized in haystack:
            return True
        return normalized.rsplit("/", 1)[-1] in haystack

    def synthesize(
        self,
        findings: tuple[MisalignmentFinding, ...],
        cross_agent_votes: tuple[MisalignmentPattern, ...] = (),
    ) -> MisalignmentPattern | None:
        """Pick the strongest finding, breaking ties by the agent majority."""
        if not findings:
            return None
        if cross_agent_votes:
            counts: dict[MisalignmentPattern, int] = {}
            for vote in cross_agent_votes:
                counts[vote] = counts.get(vote, 0) + 1
            majority = max(
                counts,
                key=lambda pattern: (
                    counts[pattern],
                    -self.PATTERN_ORDER.index(pattern),
                ),
            )
            if majority in {finding.pattern for finding in findings}:
                return majority
        return max(
            findings,
            key=lambda finding: (
                finding.confidence,
                -self.PATTERN_ORDER.index(finding.pattern),
            ),
        ).pattern

    def code_verify(
        self,
        task: TaskRecord,
        findings: tuple[MisalignmentFinding, ...],
    ) -> CleaningDecision:
        """Reject on a code-level contradiction, not on a prose guess."""
        patterns = {finding.pattern for finding in findings}
        if MisalignmentPattern.TEST_ONLY_CHANGE in patterns:
            return CleaningDecision(
                False,
                findings,
                "pull request changes only tests; no source behaviour is implemented",
            )
        if MisalignmentPattern.PATCH_SCOPE_MISMATCH in patterns and not task.issue_terms:
            return CleaningDecision(
                False,
                findings,
                "patch symbols and issue terms have no shared anchor",
            )
        if MisalignmentPattern.UNRELATED_MODULE in patterns and len(task.patch_paths) > 3:
            return CleaningDecision(
                False,
                findings,
                "issue does not name a module touched by this multi-file patch",
            )
        if MisalignmentPattern.UNDER_SPECIFIED_ISSUE in patterns and len(task.patch_paths) > 1:
            return CleaningDecision(
                False,
                findings,
                "under-specified issue cannot justify a multi-file patch",
            )
        return CleaningDecision(True, findings, "PR and issue are code-aligned")

    def clean(
        self,
        tasks: tuple[TaskRecord, ...],
        votes: dict[str, tuple[MisalignmentPattern, ...]] | None = None,
    ) -> CleaningReport:
        if not tasks:
            raise PaicheckerError("at least one task is required")
        decisions: list[CleaningDecision] = []
        for task in tasks:
            findings = self.detect(task)
            if votes and task.instance_id in votes:
                self.synthesize(findings, votes[task.instance_id])
            decisions.append(self.code_verify(task, findings))
        return CleaningReport(tuple(decisions))


class SimulatorRole(str, Enum):
    MIND_CHANGER = "mind_changer"
    EXPERT_NITPICKER = "expert_nitpicker"


@dataclass(frozen=True)
class UserTurn:
    turn: int
    role: SimulatorRole
    prompt: str
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.turn <= 0:
            raise PaicheckerError("turn must be positive")
        if not self.prompt.strip():
            raise PaicheckerError("prompt must not be empty")


@dataclass(frozen=True)
class SessionValidation:
    turn: int
    valid: bool
    missing_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...]


@dataclass(frozen=True)
class SessionReport:
    validations: tuple[SessionValidation, ...]

    @property
    def passed(self) -> bool:
        return all(validation.valid for validation in self.validations)


class DynamicUserSimulator:
    """A deterministic two-turn user simulation for multi-turn coding."""

    def __init__(self, initial_requirement: str, max_turns: int = 3) -> None:
        if not initial_requirement.strip():
            raise PaicheckerError("initial requirement must not be empty")
        if max_turns < 2 or max_turns > 8:
            raise PaicheckerError("max_turns must be between 2 and 8")
        self._initial = initial_requirement.strip()
        self._max_turns = max_turns

    @property
    def initial_requirement(self) -> str:
        return self._initial

    def turn(self, turn: int) -> UserTurn | None:
        if turn <= 0 or turn > self._max_turns:
            return None
        if turn == 1:
            return UserTurn(1, SimulatorRole.MIND_CHANGER, self._initial)
        if turn == 2:
            return UserTurn(
                2,
                SimulatorRole.MIND_CHANGER,
                "I changed the requirement: keep the old API but add the new filter.",
                required_terms=("old api", "filter"),
                forbidden_terms=("remove the old api",),
            )
        if turn == 3:
            return UserTurn(
                3,
                SimulatorRole.EXPERT_NITPICKER,
                "Remove the dead compatibility branch and show the regression test.",
                required_terms=("dead", "regression"),
                forbidden_terms=("leave the dead branch",),
            )
        return None

    def validate(self, turn: UserTurn, response: str) -> SessionValidation:
        lower = response.lower()
        missing = tuple(term for term in turn.required_terms if term not in lower)
        forbidden = tuple(term for term in turn.forbidden_terms if term in lower)
        return SessionValidation(
            turn=turn.turn,
            valid=not missing and not forbidden,
            missing_terms=missing,
            forbidden_terms=forbidden,
        )

    def run(self, responses: tuple[str, ...]) -> SessionReport:
        validations: list[SessionValidation] = []
        for index, response in enumerate(responses, start=1):
            turn = self.turn(index)
            if turn is None:
                break
            validations.append(self.validate(turn, response))
        return SessionReport(tuple(validations))
