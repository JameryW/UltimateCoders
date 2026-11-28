"""Multi-scope memory isolation and the memory-trap risk fence (V28 #129).

MixUA's four scopes keep a learned experience attached to the identity that
earned it.  The Memory Trap adds a second, orthogonal guard: a benign shortcut
learned in a low-risk task must not be recalled into a high-risk task, because
the shortcut can silently disable a required check.

This module is deterministic and side-effect free.  Callers register nothing;
they pass records and a task context to :class:`MixUaScopeGuard` and receive a
report with explicit reasons.  ``UC_MIXUA_SCOPE_GUARD`` is opt-in.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "MIXUA_SCOPE_GUARD_ENV",
    "Scope",
    "RiskTier",
    "MemoryRecord",
    "TaskContext",
    "ScopeDecision",
    "ScopeReport",
    "MixUaScopeGuard",
    "mixua_scope_guard_enabled",
]

MIXUA_SCOPE_GUARD_ENV = "UC_MIXUA_SCOPE_GUARD"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def mixua_scope_guard_enabled() -> bool:
    """Return whether the scoped-memory guard is enabled."""

    return os.environ.get(MIXUA_SCOPE_GUARD_ENV, "").strip().lower() in _TRUTHY


class Scope(str, Enum):
    """The four MixUA ownership scopes."""

    USER_AGENT = "user-agent"
    USER = "user"
    AGENT = "agent"
    GLOBAL = "global"


class RiskTier(int, Enum):
    """Ordered risk levels for a memory and a task context."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(frozen=True)
class MemoryRecord:
    """One recallable memory with its scope and risk provenance."""

    id: str
    scope: Scope
    risk_tier: RiskTier
    content: str
    user_id: str = ""
    agent_id: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("memory id must not be empty")
        if not isinstance(self.scope, Scope):
            raise ValueError("scope must be a Scope")
        if not isinstance(self.risk_tier, RiskTier):
            raise ValueError("risk_tier must be a RiskTier")
        if not self.content.strip():
            raise ValueError("memory content must not be empty")
        if self.scope is Scope.USER and not self.user_id.strip():
            raise ValueError("user-scoped memory requires user_id")
        if self.scope is Scope.AGENT and not self.agent_id.strip():
            raise ValueError("agent-scoped memory requires agent_id")
        if self.scope is Scope.USER_AGENT and (
            not self.user_id.strip() or not self.agent_id.strip()
        ):
            raise ValueError("user-agent memory requires user_id and agent_id")


@dataclass(frozen=True)
class TaskContext:
    """The identity and risk envelope of the task asking for memory."""

    user_id: str
    agent_id: str
    risk_tier: RiskTier
    allowed_scopes: frozenset[Scope] = frozenset(
        {Scope.USER_AGENT, Scope.USER, Scope.AGENT, Scope.GLOBAL}
    )

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("task user_id must not be empty")
        if not self.agent_id.strip():
            raise ValueError("task agent_id must not be empty")
        if not isinstance(self.risk_tier, RiskTier):
            raise ValueError("task risk_tier must be a RiskTier")
        if not self.allowed_scopes:
            raise ValueError("allowed_scopes must not be empty")
        if any(not isinstance(scope, Scope) for scope in self.allowed_scopes):
            raise ValueError("allowed_scopes must contain Scope values")


@dataclass(frozen=True)
class ScopeDecision:
    """Why one record was kept or blocked."""

    memory_id: str
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ScopeReport:
    """The complete visibility decision for one recall request."""

    decisions: tuple[ScopeDecision, ...]
    allowed: tuple[MemoryRecord, ...]
    blocked: tuple[MemoryRecord, ...]

    @property
    def clean(self) -> bool:
        return not self.blocked

    @property
    def isolation_ratio(self) -> float:
        total = len(self.allowed) + len(self.blocked)
        if not total:
            return 1.0
        return len(self.allowed) / total

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": [m.id for m in self.allowed],
            "blocked": [m.id for m in self.blocked],
            "decisions": [
                {"memory_id": d.memory_id, "allowed": d.allowed, "reason": d.reason}
                for d in self.decisions
            ],
        }


class MixUaScopeGuard:
    """Apply identity scope and the low-risk-to-high-risk memory fence."""

    def evaluate(
        self,
        records: Iterable[MemoryRecord],
        context: TaskContext,
    ) -> ScopeReport:
        decisions: list[ScopeDecision] = []
        allowed: list[MemoryRecord] = []
        blocked: list[MemoryRecord] = []
        seen: set[str] = set()

        for record in records:
            if record.id in seen:
                raise ValueError(f"duplicate memory id {record.id!r}")
            seen.add(record.id)
            reason = self._reason(record, context)
            decisions.append(
                ScopeDecision(record.id, allowed=reason is None, reason=reason or "")
            )
            if reason is None:
                allowed.append(record)
            else:
                blocked.append(record)

        return ScopeReport(tuple(decisions), tuple(allowed), tuple(blocked))

    def retain(
        self,
        records: Iterable[MemoryRecord],
        context: TaskContext,
    ) -> tuple[MemoryRecord, ...]:
        return self.evaluate(records, context).allowed

    @staticmethod
    def _reason(record: MemoryRecord, context: TaskContext) -> str | None:
        if record.scope not in context.allowed_scopes:
            return "scope_not_allowed"
        if record.scope is Scope.USER_AGENT:
            if record.user_id != context.user_id or record.agent_id != context.agent_id:
                return "identity_mismatch"
        elif record.scope is Scope.USER:
            if record.user_id != context.user_id:
                return "identity_mismatch"
        elif record.scope is Scope.AGENT:
            if record.agent_id != context.agent_id:
                return "identity_mismatch"
        if record.risk_tier.value < context.risk_tier.value:
            return "risk_tier_too_permissive"
        return None
