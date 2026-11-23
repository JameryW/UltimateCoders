"""SWE-CI continuous-integration loop (#72).

SWE-CI evaluates maintainability across long runs of commits. The ledger's
Architect/Programmer split is captured here as deterministic bookkeeping: the
architect enforces the monotone passed-test set, preserved architecture
invariants and a module-coupling ceiling; the programmer supplies the next
commit. A violating commit is rejected and the accepted pass set does not move,
so a later commit can still recover without ever losing a previously passing
test.

Enable with ``UC_SWE_CI_LOOP=1``. No git or test runner is invoked here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "ArchitectAgent",
    "ArchitectVerdict",
    "CiCommit",
    "CiReport",
    "CommitDecision",
    "ProgrammerAgent",
    "SweCiLoop",
    "swe_ci_loop_enabled",
]

SWE_CI_LOOP_ENV = "UC_SWE_CI_LOOP"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
DEFAULT_MAX_COUPLING = 0.8


def swe_ci_loop_enabled() -> bool:
    return os.environ.get(SWE_CI_LOOP_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class CiCommit:
    """One candidate commit in a CI evolution run."""

    index: int
    passed_tests: frozenset[str]
    changed_paths: tuple[str, ...] = ()
    coupling: float = 0.0
    invariants_preserved: bool = True

    def __post_init__(self) -> None:
        if self.index <= 0:
            raise ValueError("commit index must be positive")
        if not 0.0 <= self.coupling <= 1.0:
            raise ValueError("coupling must be within [0, 1]")


@dataclass(frozen=True)
class ArchitectVerdict:
    """Architect review of one candidate commit."""

    accepted: bool
    regressions: tuple[str, ...] = ()
    invariant_violation: bool = False
    coupling_exceeded: bool = False

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.regressions:
            reasons.append("regressed tests: " + ", ".join(self.regressions))
        if self.invariant_violation:
            reasons.append("architecture invariant violated")
        if self.coupling_exceeded:
            reasons.append("module coupling exceeds the ceiling")
        return tuple(reasons)


@dataclass(frozen=True)
class CommitDecision:
    """Decision for one commit."""

    index: int
    accepted: bool
    reason: str


@dataclass(frozen=True)
class CiReport:
    """Outcome of a CI run."""

    decisions: tuple[CommitDecision, ...]
    accepted: int
    zero_regression_rate: float
    final_pass_set: frozenset[str]

    @property
    def rejected(self) -> int:
        return len(self.decisions) - self.accepted

    def to_dict(self) -> dict[str, object]:
        return {
            "decisions": [
                {
                    "index": decision.index,
                    "accepted": decision.accepted,
                    "reason": decision.reason,
                }
                for decision in self.decisions
            ],
            "accepted": self.accepted,
            "rejected": self.rejected,
            "zero_regression_rate": self.zero_regression_rate,
            "final_pass_set": sorted(self.final_pass_set),
        }


class ArchitectAgent:
    """Review commits against the accepted pass set and architecture bounds."""

    def __init__(self, max_coupling: float = DEFAULT_MAX_COUPLING) -> None:
        if not 0.0 <= max_coupling <= 1.0:
            raise ValueError("max_coupling must be within [0, 1]")
        self.max_coupling = max_coupling

    def review(
        self,
        commit: CiCommit,
        previous_pass_set: frozenset[str],
    ) -> ArchitectVerdict:
        regressions = tuple(sorted(previous_pass_set - commit.passed_tests))
        return ArchitectVerdict(
            accepted=(
                not regressions
                and commit.invariants_preserved
                and commit.coupling <= self.max_coupling
            ),
            regressions=regressions,
            invariant_violation=not commit.invariants_preserved,
            coupling_exceeded=commit.coupling > self.max_coupling,
        )


class ProgrammerAgent:
    """Deterministic programmer stub; callers may substitute their own plan."""

    def prepare(self, commit: CiCommit) -> CiCommit:
        return commit


class SweCiLoop:
    """Run a sequence of commits through the architect/programmer loop."""

    def __init__(
        self,
        architect: ArchitectAgent | None = None,
        programmer: ProgrammerAgent | None = None,
    ) -> None:
        self.architect = architect or ArchitectAgent()
        self.programmer = programmer or ProgrammerAgent()

    def run(self, commits: tuple[CiCommit, ...]) -> CiReport:
        decisions: list[CommitDecision] = []
        previous_pass_set: frozenset[str] = frozenset()
        accepted = 0
        for raw in commits:
            commit = self.programmer.prepare(raw)
            verdict = self.architect.review(commit, previous_pass_set)
            if verdict.accepted:
                accepted += 1
                previous_pass_set = commit.passed_tests
                decisions.append(CommitDecision(commit.index, True, "accepted"))
            else:
                decisions.append(
                    CommitDecision(
                        commit.index,
                        False,
                        "; ".join(verdict.reasons) or "rejected by architect",
                    )
                )
        total = len(decisions)
        return CiReport(
            decisions=tuple(decisions),
            accepted=accepted,
            zero_regression_rate=(accepted / total) if total else 0.0,
            final_pass_set=previous_pass_set,
        )
