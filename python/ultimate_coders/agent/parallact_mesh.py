"""Parallel multi-agent conflict isolation and role healing (ledger item #91).

ParallACT showed that concurrent agents editing contradictory code can collapse
from a single-agent 62% solve rate to 16%. The 516-configuration meta-analysis
also found that multi-agent coordination loses to a same-budget single agent in
74% of settings unless evidence is partitioned, externally verified, or
heterogeneous. This module keeps the safety decision deterministic: callers
declare each agent's write scope and coordination evidence, and the mesh either
keeps the work parallel, serialises it, or fails a role over to a healthy peer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "PARALLACT_MESH_ENV",
    "ParallactError",
    "WriteScope",
    "AgentCapability",
    "Conflict",
    "RoleAssignment",
    "MeshPlan",
    "MeshMode",
    "ParallactMesh",
    "parallact_mesh_enabled",
]

PARALLACT_MESH_ENV = "UC_PARALLACT_MESH"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def parallact_mesh_enabled() -> bool:
    return os.environ.get(PARALLACT_MESH_ENV, "").strip().lower() in _TRUTHY


class ParallactError(ValueError):
    """Raised when a scope or capability record is malformed."""


class MeshMode(str, Enum):
    PARALLEL = "parallel"
    SERIAL = "serial"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WriteScope:
    agent_id: str
    symbols: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ParallactError("agent id must not be empty")
        object.__setattr__(
            self,
            "symbols",
            frozenset(symbol.strip() for symbol in self.symbols if symbol.strip()),
        )
        object.__setattr__(
            self,
            "paths",
            frozenset(path.strip() for path in self.paths if path.strip()),
        )

    def conflicts_with(self, other: WriteScope) -> tuple[str, ...]:
        overlaps: list[str] = []
        for symbol in sorted(self.symbols & other.symbols):
            overlaps.append(f"symbol:{symbol}")
        for path in sorted(self.paths & other.paths):
            overlaps.append(f"path:{path}")
        return tuple(overlaps)


@dataclass(frozen=True)
class AgentCapability:
    agent_id: str
    role: str
    evidence_partitioned: bool = False
    external_verified: bool = False
    error_diversity: bool = False

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ParallactError("agent id must not be empty")
        if not self.role.strip():
            raise ParallactError("role must not be empty")

    def earns_parallelism(self) -> bool:
        return self.evidence_partitioned or self.external_verified or self.error_diversity


@dataclass(frozen=True)
class Conflict:
    left: str
    right: str
    overlaps: tuple[str, ...]


@dataclass(frozen=True)
class RoleAssignment:
    role: str
    agent_id: str
    failover_from: str | None = None


@dataclass(frozen=True)
class MeshPlan:
    mode: MeshMode
    assignments: tuple[RoleAssignment, ...]
    conflicts: tuple[Conflict, ...]
    reason: str

    @property
    def parallel(self) -> bool:
        return self.mode is MeshMode.PARALLEL

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "assignments": [
                {
                    "role": assignment.role,
                    "agent_id": assignment.agent_id,
                    "failover_from": assignment.failover_from,
                }
                for assignment in self.assignments
            ],
            "conflicts": [
                {
                    "left": conflict.left,
                    "right": conflict.right,
                    "overlaps": list(conflict.overlaps),
                }
                for conflict in self.conflicts
            ],
            "reason": self.reason,
        }


class ParallactMesh:
    """Builds a conflict-aware parallel or serial execution plan."""

    def __init__(self) -> None:
        self._capabilities: dict[str, AgentCapability] = {}
        self._scopes: dict[str, WriteScope] = {}

    def register_capability(self, capability: AgentCapability) -> None:
        if capability.agent_id in self._capabilities:
            raise ParallactError(f"duplicate agent capability: {capability.agent_id}")
        self._capabilities[capability.agent_id] = capability

    def register_scope(self, scope: WriteScope) -> None:
        if scope.agent_id in self._scopes:
            raise ParallactError(f"duplicate write scope: {scope.agent_id}")
        self._scopes[scope.agent_id] = scope

    def conflicts(self) -> tuple[Conflict, ...]:
        agents = sorted(self._scopes)
        found: list[Conflict] = []
        for index, left in enumerate(agents):
            for right in agents[index + 1 :]:
                overlaps = self._scopes[left].conflicts_with(self._scopes[right])
                if overlaps:
                    found.append(Conflict(left, right, overlaps))
        return tuple(found)

    def has_parallel_benefit(self) -> bool:
        return any(capability.earns_parallelism() for capability in self._capabilities.values())

    def plan(self, failed_agents: tuple[str, ...] = ()) -> MeshPlan:
        failed = set(failed_agents)
        capabilities = tuple(self._capabilities.values())
        if not capabilities:
            return MeshPlan(MeshMode.BLOCKED, (), (), "no agent capabilities registered")

        conflicts = self.conflicts()
        if conflicts:
            return MeshPlan(
                MeshMode.SERIAL,
                self._assign_without_failover(failed),
                conflicts,
                "overlapping symbol or path write scopes",
            )

        if not self.has_parallel_benefit():
            return MeshPlan(
                MeshMode.SERIAL,
                self._assign_without_failover(failed),
                (),
                "no evidence partition, external verification, or error diversity",
            )

        assignments, missing = self._assign_with_failover(failed)
        if missing:
            return MeshPlan(
                MeshMode.BLOCKED,
                assignments,
                (),
                "no healthy peer for role(s): " + ", ".join(sorted(missing)),
            )
        return MeshPlan(MeshMode.PARALLEL, assignments, (), "coordination evidence present")

    def _assign_without_failover(self, failed: set[str]) -> tuple[RoleAssignment, ...]:
        assignments = []
        for capability in sorted(self._capabilities.values(), key=lambda item: item.agent_id):
            if capability.agent_id in failed:
                continue
            assignments.append(RoleAssignment(capability.role, capability.agent_id))
        return tuple(assignments)

    def _assign_with_failover(
        self, failed: set[str]
    ) -> tuple[tuple[RoleAssignment, ...], set[str]]:
        by_role: dict[str, list[AgentCapability]] = {}
        for capability in sorted(self._capabilities.values(), key=lambda item: item.agent_id):
            by_role.setdefault(capability.role, []).append(capability)
        assignments: list[RoleAssignment] = []
        missing: set[str] = set()
        for role in sorted(by_role):
            candidates = by_role[role]
            healthy = [candidate for candidate in candidates if candidate.agent_id not in failed]
            if healthy:
                chosen = healthy[0]
                failover_from = (
                    next(
                        (
                            candidate.agent_id
                            for candidate in candidates
                            if candidate.agent_id in failed
                        ),
                        None,
                    )
                    if chosen.agent_id != candidates[0].agent_id
                    else None
                )
                assignments.append(RoleAssignment(role, chosen.agent_id, failover_from))
            else:
                missing.add(role)
        return tuple(assignments), missing
