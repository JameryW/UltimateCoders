"""Shadow MCP routing and resource-amplification guards (ledger item #100).

The router scores candidate tool invocations before dispatch using task
preservation, operation pressure, and route geometry drift. Security and audit
tools are mandatory and cannot be skipped by a shadow invocation. A polling
guard provides exponential backoff and a monotone completion bound.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

__all__ = [
    "SHADOW_MCP_ROUTER_ENV",
    "SodsFeatures",
    "ToolCandidate",
    "RouteDecision",
    "ShadowMcpRouter",
    "PollingGuard",
    "shadow_mcp_router_enabled",
]

SHADOW_MCP_ROUTER_ENV = "UC_SHADOW_MCP_ROUTER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def shadow_mcp_router_enabled() -> bool:
    return os.environ.get(SHADOW_MCP_ROUTER_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class SodsFeatures:
    """SODS score components in [0, 1]."""

    task_preservation: float
    operation_pressure: float
    route_drift: float

    def __post_init__(self) -> None:
        for name in ("task_preservation", "operation_pressure", "route_drift"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")

    @property
    def score(self) -> float:
        return (
            0.5 * self.task_preservation
            + 0.25 * (1.0 - self.operation_pressure)
            + 0.25 * (1.0 - self.route_drift)
        )


@dataclass(frozen=True)
class ToolCandidate:
    tool: str
    features: SodsFeatures
    security_critical: bool = False
    audit_required: bool = False
    shadowed: bool = False
    allowed: bool = True

    def __post_init__(self) -> None:
        if not self.tool.strip():
            raise ValueError("tool must not be empty")


@dataclass(frozen=True)
class RouteDecision:
    selected: str | None
    blocked: tuple[str, ...]
    skipped_security: tuple[str, ...]
    reason: str

    @property
    def ok(self) -> bool:
        return self.selected is not None and not self.skipped_security


@dataclass
class ShadowMcpRouter:
    """Deterministic pre-dispatch router with mandatory security tools."""

    minimum_score: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_score <= 1.0:
            raise ValueError("minimum_score must be in [0, 1]")

    def route(self, candidates: tuple[ToolCandidate, ...] | list[ToolCandidate]) -> RouteDecision:
        if not candidates:
            return RouteDecision(None, (), (), "no candidates")
        blocked = tuple(
            candidate.tool
            for candidate in candidates
            if candidate.shadowed or not candidate.allowed
        )
        eligible = [
            candidate
            for candidate in candidates
            if not candidate.shadowed
            and candidate.allowed
            and candidate.features.score >= self.minimum_score
        ]
        security = [
            candidate
            for candidate in candidates
            if candidate.security_critical or candidate.audit_required
        ]
        skipped_security = tuple(
            candidate.tool for candidate in security if candidate.shadowed or not candidate.allowed
        )
        if skipped_security:
            return RouteDecision(
                None,
                blocked,
                skipped_security,
                "mandatory security or audit tool cannot be skipped",
            )
        if not eligible:
            return RouteDecision(None, blocked, (), "no eligible candidate")
        selected = max(eligible, key=lambda candidate: (candidate.features.score, candidate.tool))
        reason = "selected highest SODS score"
        if security:
            selected = max(
                security + [selected],
                key=lambda candidate: (candidate.features.score, candidate.tool),
            )
            reason = "security tool preserved in route"
        return RouteDecision(selected.tool, blocked, (), reason)


@dataclass
class PollingGuard:
    """Exponential backoff and monotone completion bound for state polling."""

    base_interval_steps: int = 1
    max_interval_steps: int = 32
    completion_bound_steps: int = 64
    _last_state: str | None = field(default=None, init=False)
    _repeat_count: int = field(default=0, init=False)
    _first_step: int | None = field(default=None, init=False)
    _done: bool = field(default=False, init=False)

    def observe(self, step: int, state_digest: str, *, task_complete: bool = False) -> int | None:
        """Return the next permitted poll step, or None if polling must stop."""
        if task_complete:
            self._done = True
            return None
        if self._first_step is None:
            self._first_step = step
        if self._done:
            return None
        if step - self._first_step > self.completion_bound_steps:
            return None
        if state_digest == self._last_state:
            self._repeat_count += 1
        else:
            self._last_state = state_digest
            self._repeat_count = 0
        interval = min(
            self.max_interval_steps,
            self.base_interval_steps * (2 ** min(self._repeat_count, 10)),
        )
        return step + interval

    def reset(self) -> None:
        self._last_state = None
        self._repeat_count = 0
        self._first_step = None
        self._done = False

    @property
    def done(self) -> bool:
        return self._done
