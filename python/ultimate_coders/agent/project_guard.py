"""ProjectGuard (#42) — progressive specification tracking for long sessions.

The guard keeps an external, bounded state layer for requirements that emerge
over many turns. Each disclosure becomes an atomic verifiable component with
its own keywords and cross-component dependencies. After every incremental
edit the caller rescans the patch and tests; missing components and unresolved
dependency edges are reported, so a 60-turn conversation cannot silently
forget an earlier requirement.

Ledger item: #42 (ProjectGuard, ResearchGate:417216396).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AtomicComponent",
    "ComponentState",
    "ConflictEdge",
    "DisclosureResult",
    "GuardScan",
    "NormKind",
    "PROJECT_GUARD_ENABLED_ENV",
    "ProjectGuard",
    "project_guard_enabled",
]

PROJECT_GUARD_ENABLED_ENV = "UC_PROJECT_GUARD"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def project_guard_enabled() -> bool:
    """Whether the opt-in ProjectGuard state layer is enabled."""
    return os.environ.get(PROJECT_GUARD_ENABLED_ENV, "").strip().lower() in _TRUTHY


class NormKind(str, Enum):
    """Kind of atomic specification component."""

    REQUIREMENT = "requirement"
    CONSTRAINT = "constraint"
    ACCEPTANCE = "acceptance"


def _derive_keywords(text: str, limit: int = 6) -> tuple[str, ...]:
    words = [
        token.lower()
        for token in _TOKEN.findall(text)
        if len(token) >= 3
    ]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return tuple(seen)


def _keyword_present(keyword: str, evidence: str) -> bool:
    """Match a keyword exactly or by a stable stem prefix (e.g. invalidat-)."""
    if keyword in evidence:
        return True
    return len(keyword) >= 6 and keyword[:6] in evidence


@dataclass(frozen=True)
class AtomicComponent:
    """One atomic, verifiable component of the emerging specification."""

    id: str
    text: str
    kind: NormKind = NormKind.REQUIREMENT
    keywords: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("component id must not be empty")
        if not self.text.strip():
            raise ValueError("component text must not be empty")
        keywords = tuple(
            keyword.strip().lower()
            for keyword in (self.keywords or _derive_keywords(self.text))
            if keyword.strip()
        )
        if not keywords:
            raise ValueError("component must expose at least one keyword")
        dependencies = tuple(dependency for dependency in self.dependencies)
        object.__setattr__(self, "keywords", keywords)
        object.__setattr__(self, "dependencies", dependencies)


@dataclass(frozen=True)
class ComponentState:
    """Active/superseded state for one disclosed component."""

    component: AtomicComponent
    disclosed_turn: int
    status: str = "active"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.component.id,
            "text": self.component.text,
            "kind": self.component.kind.value,
            "keywords": list(self.component.keywords),
            "dependencies": list(self.component.dependencies),
            "required": self.component.required,
            "disclosed_turn": self.disclosed_turn,
            "status": self.status,
        }


@dataclass(frozen=True)
class ConflictEdge:
    """A missing dependency edge in the cross-turn specification graph."""

    source: str
    target: str
    reason: str = "missing_dependency"

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "reason": self.reason}


@dataclass(frozen=True)
class DisclosureResult:
    """Result of one progressive-disclosure turn."""

    turn: int
    added: tuple[str, ...]
    superseded: tuple[str, ...]
    conflicts: tuple[ConflictEdge, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "turn": self.turn,
            "added": list(self.added),
            "superseded": list(self.superseded),
            "conflicts": [edge.to_dict() for edge in self.conflicts],
        }


@dataclass(frozen=True)
class GuardScan:
    """Faithfulness scan after one incremental edit."""

    turn: int
    faithful: bool
    fidelity: float
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    conflicts: tuple[ConflictEdge, ...]
    changed_paths: tuple[str, ...]
    required_count: int

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    def to_dict(self) -> dict[str, object]:
        return {
            "turn": self.turn,
            "faithful": self.faithful,
            "fidelity": self.fidelity,
            "matched": list(self.matched),
            "missing": list(self.missing),
            "conflicts": [edge.to_dict() for edge in self.conflicts],
            "changed_paths": list(self.changed_paths),
            "required_count": self.required_count,
        }


class ProjectGuard:
    """Bounded external state layer for progressive requirement tracking."""

    def __init__(self, *, max_turns: int = 60, max_scans: int = 256) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        if max_scans <= 0:
            raise ValueError("max_scans must be positive")
        self.max_turns = max_turns
        self.max_scans = max_scans
        self._components: dict[str, ComponentState] = {}
        self._last_turn = 0
        self._scans: list[GuardScan] = []

    @property
    def last_turn(self) -> int:
        return self._last_turn

    @property
    def scans(self) -> tuple[GuardScan, ...]:
        return tuple(self._scans)

    def active_components(self) -> tuple[AtomicComponent, ...]:
        return tuple(
            state.component
            for state in self._components.values()
            if state.status == "active"
        )

    def component_state(self, component_id: str) -> ComponentState | None:
        return self._components.get(component_id)

    def disclose(
        self,
        turn: int,
        components: Sequence[AtomicComponent],
    ) -> DisclosureResult:
        """Add or supersede components for one conversation turn."""
        self._validate_turn(turn)
        if turn <= self._last_turn:
            raise ValueError("disclosure turn must increase monotonically")
        added: list[str] = []
        superseded: list[str] = []
        for component in components:
            previous = self._components.get(component.id)
            if previous is not None:
                superseded.append(component.id)
            else:
                added.append(component.id)
            self._components[component.id] = ComponentState(
                component=component,
                disclosed_turn=turn,
                status="active",
            )
        self._last_turn = turn
        conflicts = self.conflict_graph()
        return DisclosureResult(
            turn=turn,
            added=tuple(added),
            superseded=tuple(superseded),
            conflicts=conflicts,
        )

    def conflict_graph(self) -> tuple[ConflictEdge, ...]:
        """Dependencies that point at components not currently active."""
        active = {component.id for component in self.active_components()}
        conflicts: list[ConflictEdge] = []
        for component in self.active_components():
            for dependency in component.dependencies:
                if dependency not in active:
                    conflicts.append(
                        ConflictEdge(source=component.id, target=dependency)
                    )
        return tuple(conflicts)

    def scan(
        self,
        turn: int,
        patch_text: str,
        *,
        changed_paths: Sequence[str] = (),
        tests: Sequence[str] = (),
    ) -> GuardScan:
        """Rescan the emerging spec after one incremental edit."""
        self._validate_turn(turn)
        evidence = " ".join([patch_text, *[str(test) for test in tests]]).lower()
        required = [
            component
            for component in self.active_components()
            if component.required
        ]
        matched: list[str] = []
        missing: list[str] = []
        for component in required:
            if all(_keyword_present(keyword, evidence) for keyword in component.keywords):
                matched.append(component.id)
            else:
                missing.append(component.id)
        fidelity = len(matched) / len(required) if required else 1.0
        conflicts = self.conflict_graph()
        scan = GuardScan(
            turn=turn,
            faithful=not missing and not conflicts,
            fidelity=fidelity,
            matched=tuple(matched),
            missing=tuple(missing),
            conflicts=conflicts,
            changed_paths=tuple(str(path) for path in changed_paths),
            required_count=len(required),
        )
        self._scans.append(scan)
        if len(self._scans) > self.max_scans:
            del self._scans[: len(self._scans) - self.max_scans]
        self._last_turn = max(self._last_turn, turn)
        return scan

    def _validate_turn(self, turn: int) -> None:
        if not isinstance(turn, int) or turn <= 0:
            raise ValueError("turn must be a positive integer")
        if turn > self.max_turns:
            raise ValueError("turn exceeds the configured maximum")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_turns": self.max_turns,
            "last_turn": self._last_turn,
            "active": [state.to_dict() for state in self._components.values()],
            "scans": [scan.to_dict() for scan in self._scans],
        }
