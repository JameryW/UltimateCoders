"""Same-repository focus resumption and behavioral trajectory fingerprints.

Ledger item #135: interleaving several unfinished tasks in one repository makes
agents lose the active task's state and history.  `FocusResumption` keeps one
state card per branch/issue and filters a resumed turn to the history that is
relevant to that card.  `BehaviorFingerprint` is the deterministic side: it
parses tool events into exploration / editing / testing proportions without
depending on task outcomes or token counts.

Deterministic bookkeeping only; the caller supplies events and task state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "FOCUS_RESUMPTION_ENV",
    "TaskStateCard",
    "SwitchResult",
    "FocusResumption",
    "BehaviorPhase",
    "BehaviorEvent",
    "BehaviorFingerprint",
    "behavior_fingerprint",
    "focus_resumption_enabled",
]

FOCUS_RESUMPTION_ENV = "UC_FOCUS_RESUMPTION"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def focus_resumption_enabled() -> bool:
    return os.environ.get(FOCUS_RESUMPTION_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TaskStateCard:
    """A compact state snapshot for one branch or issue."""

    task_id: str
    branch: str
    issue: str
    goals: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    open_steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.task_id, self.branch, self.issue):
            if not value.strip():
                raise ValueError("task_id, branch, and issue must not be empty")
        if not self.goals:
            raise ValueError("goals must not be empty")

    def summary(self) -> str:
        return (
            f"[{self.task_id}] branch={self.branch} issue={self.issue}\n"
            f"goals: {'; '.join(self.goals)}\n"
            f"files: {', '.join(self.files) or '-'}\n"
            f"decisions: {'; '.join(self.decisions) or '-'}\n"
            f"open: {'; '.join(self.open_steps) or '-'}"
        )

    def relevance(self, text: str) -> int:
        haystack = text.lower()
        terms = (*self.goals, *self.files, *self.decisions, self.issue)
        return sum(1 for term in terms if term.lower() in haystack)


@dataclass(frozen=True)
class SwitchResult:
    task_id: str
    card: TaskStateCard
    kept_history: tuple[str, ...]
    dropped_history: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "summary": self.card.summary(),
            "kept": list(self.kept_history),
            "dropped": list(self.dropped_history),
        }


class FocusResumption:
    """Maintains per-task cards and filters history on a focus switch."""

    def __init__(self, max_cards: int = 64) -> None:
        if max_cards <= 0:
            raise ValueError("max_cards must be positive")
        self._max_cards = max_cards
        self._cards: dict[str, TaskStateCard] = {}
        self._history: dict[str, list[str]] = {}

    def register(self, card: TaskStateCard) -> None:
        if card.task_id not in self._cards and len(self._cards) >= self._max_cards:
            raise ValueError("card capacity reached")
        self._cards[card.task_id] = card
        self._history.setdefault(card.task_id, [])

    def record(self, task_id: str, line: str) -> None:
        if task_id not in self._cards:
            raise KeyError(f"unknown task: {task_id}")
        self._history[task_id].append(line)

    def switch(self, task_id: str, history: tuple[str, ...] | None = None) -> SwitchResult:
        card = self._cards.get(task_id)
        if card is None:
            raise KeyError(f"unknown task: {task_id}")
        pool = tuple(history) if history is not None else tuple(self._history[task_id])
        kept = tuple(line for line in pool if card.relevance(line) > 0)
        dropped = tuple(line for line in pool if card.relevance(line) == 0)
        return SwitchResult(task_id, card, kept, dropped)

    def card(self, task_id: str) -> TaskStateCard | None:
        return self._cards.get(task_id)

    def active_tasks(self) -> tuple[str, ...]:
        return tuple(sorted(self._cards))


class BehaviorPhase(str, Enum):
    EXPLORATION = "exploration"
    EDITING = "editing"
    TESTING = "testing"


@dataclass(frozen=True)
class BehaviorEvent:
    phase: BehaviorPhase
    tool: str = ""

    def __post_init__(self) -> None:
        if self.phase is BehaviorPhase.EXPLORATION and not self.tool.strip():
            raise ValueError("exploration events need a tool")


@dataclass(frozen=True)
class BehaviorFingerprint:
    exploration: float
    editing: float
    testing: float
    total: int

    def __post_init__(self) -> None:
        for value in (self.exploration, self.editing, self.testing):
            if not 0.0 <= value <= 1.0:
                raise ValueError("shares must be between 0 and 1")

    def shares(self) -> dict[str, float]:
        return {
            "exploration": self.exploration,
            "editing": self.editing,
            "testing": self.testing,
        }

    def drift_from(self, other: BehaviorFingerprint, threshold: float = 0.2) -> bool:
        if threshold < 0:
            raise ValueError("threshold must not be negative")
        return any(
            abs(a - b) > threshold
            for a, b in zip(self.shares().values(), other.shares().values())
        )


def behavior_fingerprint(events: tuple[BehaviorEvent, ...]) -> BehaviorFingerprint:
    if not events:
        return BehaviorFingerprint(0.0, 0.0, 0.0, 0)
    counts = {phase: 0 for phase in BehaviorPhase}
    for event in events:
        counts[event.phase] += 1
    total = len(events)
    return BehaviorFingerprint(
        counts[BehaviorPhase.EXPLORATION] / total,
        counts[BehaviorPhase.EDITING] / total,
        counts[BehaviorPhase.TESTING] / total,
        total,
    )
