"""SkillForge cold-start distillation for a repository with no history.

Implements ledger item #63 (SkillForge, arXiv:2608.18933). The module turns
tested functions into deterministic synthetic issues, records the repair
trajectory, and distils two skill layers:

* global diagnostic skills, stored in the project index;
* local intervention skills bound to one AST symbol.

A local skill is injected only when the current task references that symbol.
The module does not execute tests, mutate a repository, or write to TiKV; it is
the planning/bookkeeping layer for the caller that does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CardScope",
    "ColdStartReport",
    "MutationKind",
    "RepairTrace",
    "SKILLFORGE_COLDSTART_ENV",
    "SkillCard",
    "SkillForgeColdStart",
    "SyntheticIssue",
    "TestedFunction",
    "skillforge_coldstart_enabled",
]

SKILLFORGE_COLDSTART_ENV = "UC_SKILLFORGE_COLDSTART"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def skillforge_coldstart_enabled() -> bool:
    return os.environ.get(SKILLFORGE_COLDSTART_ENV, "").strip().lower() in _TRUTHY


class SkillForgeError(ValueError):
    """Malformed repository or repair evidence."""


class MutationKind(str, Enum):
    OFF_BY_ONE = "off_by_one"
    NONE_RETURN = "none_return"
    WRONG_OPERATOR = "wrong_operator"


class CardScope(str, Enum):
    GLOBAL = "global"
    LOCAL = "local"


@dataclass(frozen=True)
class TestedFunction:
    """A function covered by at least one existing test."""

    symbol: str
    path: str
    tests: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.path.strip():
            raise SkillForgeError("symbol and path must not be empty")
        if not self.tests:
            raise SkillForgeError("a tested function needs at least one test")
        if any(not test.strip() for test in self.tests):
            raise SkillForgeError("test names must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "path": self.path,
            "tests": list(self.tests),
        }


@dataclass(frozen=True)
class SyntheticIssue:
    """A controlled behavioural fault derived from one tested function."""

    issue_id: str
    symbol: str
    path: str
    mutation: MutationKind
    description: str
    source_test: str

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "symbol": self.symbol,
            "path": self.path,
            "mutation": self.mutation.value,
            "description": self.description,
            "source_test": self.source_test,
        }


@dataclass(frozen=True)
class RepairTrace:
    """The evidence from one synthetic-issue repair attempt."""

    issue_id: str
    symbol: str
    path: str
    succeeded: bool
    steps: tuple[str, ...]
    failure_signature: str = ""

    def __post_init__(self) -> None:
        if not self.issue_id.strip() or not self.symbol.strip():
            raise SkillForgeError("trace id and symbol must not be empty")
        if not self.steps:
            raise SkillForgeError("a repair trace needs at least one step")


@dataclass(frozen=True)
class SkillCard:
    """One distilled skill. Local cards carry the owning symbol."""

    name: str
    scope: CardScope
    guidance: str
    evidence: tuple[str, ...]
    symbol: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.guidance.strip():
            raise SkillForgeError("skill name and guidance must not be empty")
        if self.scope is CardScope.LOCAL and not (self.symbol or "").strip():
            raise SkillForgeError("a local skill needs a symbol")
        if self.scope is CardScope.GLOBAL and self.symbol:
            raise SkillForgeError("a global skill cannot carry a symbol")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "scope": self.scope.value,
            "guidance": self.guidance,
            "evidence": list(self.evidence),
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class ColdStartReport:
    issues: tuple[SyntheticIssue, ...]
    cards: tuple[SkillCard, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "cards": [card.to_dict() for card in self.cards],
        }


class SkillForgeColdStart:
    """Deterministic synthetic-issue generation and skill distillation."""

    def __init__(self, max_issues: int = 16, max_cards: int = 32) -> None:
        if max_issues <= 0 or max_cards <= 0:
            raise SkillForgeError("capacities must be positive")
        self._max_issues = max_issues
        self._max_cards = max_cards
        self._cards: list[SkillCard] = []

    @property
    def cards(self) -> tuple[SkillCard, ...]:
        return tuple(self._cards)

    def generate(self, functions: tuple[TestedFunction, ...]) -> tuple[SyntheticIssue, ...]:
        """Generate one issue per tested function, capped by the budget."""
        if not functions:
            raise SkillForgeError("at least one tested function is required")
        symbols = [function.symbol for function in functions]
        if len(set(symbols)) != len(symbols):
            raise SkillForgeError("tested function symbols must be unique")
        mutations = tuple(MutationKind)
        issues: list[SyntheticIssue] = []
        for index, function in enumerate(functions[: self._max_issues]):
            mutation = mutations[index % len(mutations)]
            issues.append(
                SyntheticIssue(
                    issue_id=f"synthetic-{index + 1}",
                    symbol=function.symbol,
                    path=function.path,
                    mutation=mutation,
                    description=(
                        f"Regression in {function.symbol}: {mutation.value} "
                        f"while running {function.tests[0]}"
                    ),
                    source_test=function.tests[0],
                )
            )
        return tuple(issues)

    def distill(self, trace: RepairTrace) -> tuple[SkillCard, ...]:
        """Distil a successful repair into one global and one local card."""
        if not trace.succeeded:
            return ()
        evidence = tuple(trace.steps)
        global_card = SkillCard(
            name="reproduce-before-edit",
            scope=CardScope.GLOBAL,
            guidance=(
                "For a synthetic regression, reproduce the failing test first, "
                "then read the mutated function before editing."
            ),
            evidence=(trace.issue_id, *evidence),
        )
        local_card = SkillCard(
            name=f"avoid-{trace.symbol}-regression",
            scope=CardScope.LOCAL,
            guidance=(
                f"When repairing {trace.symbol}, preserve its existing aliases "
                "and boundary behaviour; re-run the source test after the edit."
            ),
            evidence=(trace.issue_id, *evidence),
            symbol=trace.symbol,
        )
        return (global_card, local_card)

    def register(self, cards: tuple[SkillCard, ...]) -> int:
        """Register cards without duplicating an existing name/scope pair."""
        for card in cards:
            if len(self._cards) >= self._max_cards:
                break
            if any(
                existing.name == card.name and existing.scope == card.scope
                for existing in self._cards
            ):
                continue
            self._cards.append(card)
        return len(self._cards)

    def inject(self, symbol: str, limit: int = 3) -> str:
        """Render the local skill for *symbol* first, then global skills."""
        if limit <= 0:
            raise SkillForgeError("limit must be positive")
        if not symbol.strip():
            raise SkillForgeError("symbol must not be empty")
        local = [
            card
            for card in self._cards
            if card.scope is CardScope.LOCAL and card.symbol == symbol
        ]
        global_cards = [card for card in self._cards if card.scope is CardScope.GLOBAL]
        selected = (local + global_cards)[:limit]
        if not selected:
            return ""
        lines = ["## Project skills (SkillForge)"]
        for card in selected:
            lines.append(f"- [{card.scope.value}] {card.name}: {card.guidance}")
        return "\n".join(lines)
