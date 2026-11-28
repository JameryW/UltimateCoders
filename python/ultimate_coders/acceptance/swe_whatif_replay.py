"""Counterfactual user-session replay and SWE-CoWork evaluation (ledger item #83).

Recorded developer sessions are reduced to pushback points. A policy can be
replayed against those points to estimate whether it would have avoided the
user's correction. The module also exposes a small organizational-role
benchmark so the same evidence can be aggregated beyond a single user.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "SWE_WHATIF_REPLAY_ENV",
    "SweWhatIfError",
    "SessionTurn",
    "RecordedSession",
    "PushbackPoint",
    "ReplayResult",
    "ReplayReport",
    "UserPreferenceChange",
    "CounterfactualReplay",
    "OrganizationRole",
    "OrganizationTask",
    "OrganizationReport",
    "swe_whatif_replay_enabled",
]

SWE_WHATIF_REPLAY_ENV = "UC_SWE_WHATIF_REPLAY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def swe_whatif_replay_enabled() -> bool:
    return os.environ.get(SWE_WHATIF_REPLAY_ENV, "").strip().lower() in _TRUTHY


class SweWhatIfError(ValueError):
    """Raised when a recorded session cannot be replayed."""


@dataclass(frozen=True)
class SessionTurn:
    index: int
    role: str
    text: str
    pushback: bool = False

    def __post_init__(self) -> None:
        if self.index < 0:
            raise SweWhatIfError("turn index must be non-negative")
        if self.role not in {"user", "assistant", "tool"}:
            raise SweWhatIfError(f"unknown role: {self.role}")
        if not self.text.strip():
            raise SweWhatIfError("turn text must not be empty")


@dataclass(frozen=True)
class RecordedSession:
    session_id: str
    turns: tuple[SessionTurn, ...]

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise SweWhatIfError("session id must not be empty")
        if not self.turns:
            raise SweWhatIfError("session must contain turns")
        indexes = [turn.index for turn in self.turns]
        if indexes != sorted(indexes) or len(set(indexes)) != len(indexes):
            raise SweWhatIfError("turn indexes must be unique and sorted")


@dataclass(frozen=True)
class PushbackPoint:
    turn_index: int
    previous_action: str
    user_response: str
    trigger: str


def extract_pushback_points(session: RecordedSession) -> tuple[PushbackPoint, ...]:
    points: list[PushbackPoint] = []
    for position, turn in enumerate(session.turns):
        if not turn.pushback:
            continue
        previous = next(
            (item for item in reversed(session.turns[:position]) if item.role == "assistant"),
            None,
        )
        points.append(
            PushbackPoint(
                turn_index=turn.index,
                previous_action=previous.text if previous else "",
                user_response=turn.text,
                trigger=turn.text.lower().split()[0] if turn.text.split() else "pushback",
            )
        )
    return tuple(points)


@dataclass(frozen=True)
class ReplayResult:
    point: PushbackPoint
    action: str
    avoided: bool
    rationale: str


@dataclass(frozen=True)
class ReplayReport:
    session_id: str
    results: tuple[ReplayResult, ...]

    @property
    def avoidance_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for result in self.results if result.avoided) / len(self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "avoidance_rate": self.avoidance_rate,
            "results": [
                {
                    "turn_index": result.point.turn_index,
                    "action": result.action,
                    "avoided": result.avoided,
                    "rationale": result.rationale,
                }
                for result in self.results
            ],
        }


class CounterfactualReplay:
    """Replay pushback points against a deterministic policy callback."""

    def __init__(self, policy: Callable[[PushbackPoint], str] | None = None) -> None:
        self._policy = policy or (lambda point: point.previous_action)

    def replay(self, session: RecordedSession) -> ReplayReport:
        points = extract_pushback_points(session)
        results: list[ReplayResult] = []
        for point in points:
            action = self._policy(point)
            avoided = bool(action.strip()) and action.strip() != point.previous_action.strip()
            results.append(
                ReplayResult(
                    point=point,
                    action=action,
                    avoided=avoided,
                    rationale="policy chose a different action after pushback point"
                    if avoided
                    else "policy repeated the action that triggered pushback",
                )
            )
        return ReplayReport(session.session_id, tuple(results))


@dataclass(frozen=True)
class UserPreferenceChange:
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class OrganizationRole:
    name: str
    responsibility: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.responsibility.strip():
            raise SweWhatIfError("role name and responsibility are required")


@dataclass(frozen=True)
class OrganizationTask:
    task_id: str
    required_roles: tuple[str, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise SweWhatIfError("task id must not be empty")
        if not self.required_roles:
            raise SweWhatIfError("organization task requires roles")


@dataclass(frozen=True)
class OrganizationReport:
    task_id: str
    coverage: float
    missing_roles: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_roles


def evaluate_organization(
    task: OrganizationTask, roles: Iterable[OrganizationRole]
) -> OrganizationReport:
    available = {role.name for role in roles}
    missing = tuple(role for role in task.required_roles if role not in available)
    coverage = 1.0 - len(missing) / len(task.required_roles)
    return OrganizationReport(task.task_id, coverage, missing)
