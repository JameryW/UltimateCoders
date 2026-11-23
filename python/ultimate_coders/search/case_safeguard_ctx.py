"""Security-guard-first repository context (ledger item #94).

CASE-Bench found that coding agents notice a repository's authentication,
input-sanitisation, and bounds checks in only 27%-36% of trajectories; omitting
them costs 8.1 percentage points of secure pass rate, and a single unsafe
example can raise exploitable code by 6.3 points. This module builds a small
call graph and puts every guard upstream of the target symbol at the top of
the context, ahead of feature code and unsafe examples.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CASE_SAFEGUARD_CTX_ENV",
    "CaseSafeguardError",
    "GuardKind",
    "GuardFunction",
    "CallEdge",
    "GuardHit",
    "GuardContext",
    "GuardIndex",
    "CaseSafeguardContext",
    "case_safeguard_ctx_enabled",
]

CASE_SAFEGUARD_CTX_ENV = "UC_CASE_SAFEGUARD_CTX"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_KIND_PRIORITY = {"auth": 0, "input_sanitization": 1, "bounds_check": 2}


def case_safeguard_ctx_enabled() -> bool:
    return os.environ.get(CASE_SAFEGUARD_CTX_ENV, "").strip().lower() in _TRUTHY


class CaseSafeguardError(ValueError):
    """Raised when a guard or call edge is malformed."""


class GuardKind(str, Enum):
    AUTH = "auth"
    INPUT_SANITIZATION = "input_sanitization"
    BOUNDS_CHECK = "bounds_check"


@dataclass(frozen=True)
class GuardFunction:
    symbol: str
    path: str
    kind: GuardKind
    source: str = ""

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise CaseSafeguardError("guard symbol must not be empty")
        if not self.path.strip():
            raise CaseSafeguardError("guard path must not be empty")


@dataclass(frozen=True)
class CallEdge:
    caller: str
    callee: str

    def __post_init__(self) -> None:
        if not self.caller.strip() or not self.callee.strip():
            raise CaseSafeguardError("call edge endpoints must not be empty")
        if self.caller.strip() == self.callee.strip():
            raise CaseSafeguardError("self call edges are not useful")


@dataclass(frozen=True)
class GuardHit:
    guard: GuardFunction
    depth: int

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.guard.symbol,
            "path": self.guard.path,
            "kind": self.guard.kind.value,
            "depth": self.depth,
            "source": self.guard.source,
        }


@dataclass(frozen=True)
class GuardContext:
    target: str
    guards: tuple[GuardHit, ...]
    header: str = "## Safety guards (must remain)"

    def render(self) -> str:
        lines = [self.header, f"Target: {self.target}"]
        for hit in self.guards:
            lines.append(
                f"- {hit.guard.kind.value}: {hit.guard.symbol} "
                f"({hit.guard.path}, depth {hit.depth})"
            )
            if hit.guard.source.strip():
                lines.append(f"  {hit.guard.source.strip()}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "header": self.header,
            "guards": [hit.to_dict() for hit in self.guards],
            "coverage": len(self.guards),
        }


class GuardIndex:
    """A tiny reverse call graph over repository symbols."""

    def __init__(self) -> None:
        self._guards: dict[str, GuardFunction] = {}
        self._callers: dict[str, set[str]] = {}
        self._callees: dict[str, set[str]] = {}

    def add_guard(self, guard: GuardFunction) -> None:
        if guard.symbol in self._guards:
            raise CaseSafeguardError(f"duplicate guard: {guard.symbol}")
        self._guards[guard.symbol] = guard

    def add_edge(self, edge: CallEdge) -> None:
        self._callees.setdefault(edge.caller, set()).add(edge.callee)
        self._callers.setdefault(edge.callee, set()).add(edge.caller)
        self._callers.setdefault(edge.caller, set())
        self._callees.setdefault(edge.callee, set())

    def guards_for(self, symbol: str) -> GuardFunction | None:
        return self._guards.get(symbol)

    def upstream_guards(self, target: str, max_depth: int = 3) -> tuple[GuardHit, ...]:
        if max_depth < 0:
            raise CaseSafeguardError("max_depth must not be negative")
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(target, 0)])
        hits: list[GuardHit] = []
        while queue:
            symbol, depth = queue.popleft()
            if symbol in seen or depth > max_depth:
                continue
            seen.add(symbol)
            guard = self._guards.get(symbol)
            if guard is not None:
                hits.append(GuardHit(guard, depth))
            if depth < max_depth:
                for caller in sorted(self._callers.get(symbol, ())):
                    queue.append((caller, depth + 1))
        hits.sort(
            key=lambda hit: (
                _KIND_PRIORITY.get(hit.guard.kind.value, 99),
                hit.depth,
                hit.guard.symbol,
            )
        )
        return tuple(hits)


class CaseSafeguardContext:
    """Builds the guard-first block for a target symbol."""

    def __init__(self, index: GuardIndex) -> None:
        self.index = index

    def build(self, target: str, max_depth: int = 3) -> GuardContext:
        if not target.strip():
            raise CaseSafeguardError("target symbol must not be empty")
        return GuardContext(target, self.index.upstream_guards(target, max_depth))

    def coverage(self, target: str, max_depth: int = 3) -> float:
        """Returns 1.0 when at least one guard is observed, else 0.0."""

        return 1.0 if self.index.upstream_guards(target, max_depth) else 0.0
