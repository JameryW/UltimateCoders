"""CodeSpec dual executable specifications (#73).

The compiler turns a set of sub-requirements plus already-collected
architecture evidence into two complementary specs:

* ``ArchitectureSpec`` records the components, their paths and the dependency
  edges that connect the feature into the existing repository.
* ``BehaviorSpec`` records the concrete behavioral clauses every component must
  satisfy.

It also emits one ``FunctionalChain`` per requirement so a later patch can be
checked for component coverage and test coverage.  This is a deterministic
bookkeeping layer: it does not parse source, call a model, or write files.

Flag: ``UC_CODESPEC_COMPILER`` (default off).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "CODESPEC_COMPILER_ENV",
    "ArchitectureSpec",
    "BehaviorSpec",
    "CodeSpecCompiler",
    "CompiledSpec",
    "ComponentEvidence",
    "FunctionalChain",
    "Requirement",
    "SpecGap",
    "ValidationReport",
    "codespec_compiler_enabled",
]

CODESPEC_COMPILER_ENV = "UC_CODESPEC_COMPILER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def codespec_compiler_enabled() -> bool:
    """Return whether the CodeSpec compiler is enabled."""

    return os.environ.get(CODESPEC_COMPILER_ENV, "").strip().lower() in _TRUTHY


def _require(value: str, field: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _words(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[A-Za-z0-9_]+", text) if part}


@dataclass(frozen=True)
class Requirement:
    """One sub-requirement in a long-horizon feature request."""

    id: str
    component: str
    behavior: str
    expected_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require(self.id, "id"))
        object.__setattr__(self, "component", _require(self.component, "component"))
        object.__setattr__(self, "behavior", _require(self.behavior, "behavior"))
        object.__setattr__(
            self,
            "expected_symbols",
            tuple(symbol.strip() for symbol in self.expected_symbols if symbol.strip()),
        )


@dataclass(frozen=True)
class ComponentEvidence:
    """Architecture evidence collected for one component."""

    component: str
    path: str
    symbols: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "component", _require(self.component, "component"))
        object.__setattr__(self, "path", _require(self.path, "path"))
        object.__setattr__(
            self,
            "symbols",
            tuple(symbol.strip() for symbol in self.symbols if symbol.strip()),
        )
        object.__setattr__(
            self,
            "dependencies",
            tuple(dep.strip() for dep in self.dependencies if dep.strip()),
        )


@dataclass(frozen=True)
class ArchitectureSpec:
    """Components and dependency edges that make up the feature chain."""

    components: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    paths: tuple[tuple[str, str], ...]

    def path_for(self, component: str) -> str | None:
        return dict(self.paths).get(component)

    def to_dict(self) -> dict[str, object]:
        return {
            "components": list(self.components),
            "edges": [list(edge) for edge in self.edges],
            "paths": dict(self.paths),
        }


@dataclass(frozen=True)
class BehaviorSpec:
    """Behavioral clauses extracted from the requirements."""

    clauses: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"clauses": list(self.clauses)}


@dataclass(frozen=True)
class FunctionalChain:
    """A single requirement's required component and symbols."""

    requirement_id: str
    component: str
    expected_symbols: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "component": self.component,
            "expected_symbols": list(self.expected_symbols),
        }


@dataclass(frozen=True)
class SpecGap:
    """A deterministic gap in the compiled specification."""

    requirement_id: str
    kind: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CompiledSpec:
    """Architecture + behavior spec plus the functional chains."""

    architecture: ArchitectureSpec
    behavior: BehaviorSpec
    chains: tuple[FunctionalChain, ...]
    gaps: tuple[SpecGap, ...]

    @property
    def complete(self) -> bool:
        return not self.gaps

    def to_dict(self) -> dict[str, object]:
        return {
            "architecture": self.architecture.to_dict(),
            "behavior": self.behavior.to_dict(),
            "chains": [chain.to_dict() for chain in self.chains],
            "gaps": [gap.to_dict() for gap in self.gaps],
            "complete": self.complete,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Result of checking a patch/test set against a compiled spec."""

    consistent: bool
    gaps: tuple[SpecGap, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "consistent": self.consistent,
            "gaps": [gap.to_dict() for gap in self.gaps],
        }


class CodeSpecCompiler:
    """Compile requirements into dual executable specs and validate them."""

    def compile(
        self,
        requirements: tuple[Requirement, ...] | list[Requirement],
        evidence: tuple[ComponentEvidence, ...] | list[ComponentEvidence],
    ) -> CompiledSpec:
        if not requirements:
            raise ValueError("at least one requirement is required")

        seen: set[str] = set()
        for requirement in requirements:
            if requirement.id in seen:
                raise ValueError(f"duplicate requirement id: {requirement.id}")
            seen.add(requirement.id)

        by_component: dict[str, ComponentEvidence] = {}
        for item in evidence:
            if item.component in by_component:
                raise ValueError(f"duplicate component evidence: {item.component}")
            by_component[item.component] = item

        chains: list[FunctionalChain] = []
        gaps: list[SpecGap] = []
        clauses: list[str] = []

        for requirement in requirements:
            clauses.append(requirement.behavior)
            component = by_component.get(requirement.component)
            if component is None:
                gaps.append(
                    SpecGap(requirement.id, "missing_component", requirement.component)
                )
            else:
                missing = sorted(
                    symbol
                    for symbol in requirement.expected_symbols
                    if symbol not in component.symbols
                )
                for symbol in missing:
                    gaps.append(SpecGap(requirement.id, "missing_symbol", symbol))
            chains.append(
                FunctionalChain(
                    requirement.id,
                    requirement.component,
                    requirement.expected_symbols,
                )
            )

        components = tuple(sorted(by_component))
        edges: list[tuple[str, str]] = []
        for component in components:
            for dep in by_component[component].dependencies:
                if dep in by_component:
                    edges.append((component, dep))
        paths = tuple(sorted((component, by_component[component].path) for component in components))

        architecture = ArchitectureSpec(components, tuple(sorted(edges)), paths)
        behavior = BehaviorSpec(tuple(sorted(set(clauses))))
        return CompiledSpec(architecture, behavior, tuple(chains), tuple(gaps))

    def validate(
        self,
        spec: CompiledSpec,
        *,
        touched_components: tuple[str, ...] = (),
        test_names: tuple[str, ...] = (),
    ) -> ValidationReport:
        touched = {component.lower() for component in touched_components}
        tests = " ".join(test_names).lower()
        gaps: list[SpecGap] = []

        if touched_components:
            for chain in spec.chains:
                if chain.component.lower() not in touched:
                    gaps.append(
                        SpecGap(
                            chain.requirement_id,
                            "unimplemented_component",
                            chain.component,
                        )
                    )

        if test_names:
            for chain in spec.chains:
                name_hint = f"{chain.requirement_id} {chain.component}".lower()
                words = _words(name_hint)
                if not words or not any(word in tests for word in words):
                    gaps.append(
                        SpecGap(
                            chain.requirement_id,
                            "untested_behavior",
                            chain.component,
                        )
                    )

        return ValidationReport(not gaps, tuple(gaps))
