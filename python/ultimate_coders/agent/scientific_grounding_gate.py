"""Executable-context alignment gate for scientific coding tasks.

Implements ledger item #66 (SWE-bench Science, arXiv:2608.19799). External
domain knowledge is useful only when its variables, shapes, dtypes, and
operator signatures can be bound to the repository's executable symbols. A
generic theorem injected without that binding can anchor an agent on the wrong
model; the gate refuses it.

The gate does not execute a formula, import a scientific package, or call a
model. Callers provide the repository symbol table.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "AlignmentReport",
    "DomainClaim",
    "GroundingError",
    "RepoBinding",
    "SCIENTIFIC_GROUNDING_GATE_ENV",
    "ScientificGroundingGate",
    "SymbolBinding",
    "scientific_grounding_enabled",
]

SCIENTIFIC_GROUNDING_GATE_ENV = "UC_SCIENTIFIC_GROUNDING_GATE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def scientific_grounding_enabled() -> bool:
    return (
        os.environ.get(SCIENTIFIC_GROUNDING_GATE_ENV, "").strip().lower() in _TRUTHY
    )


class GroundingError(ValueError):
    """Malformed domain claim or repository symbol."""


@dataclass(frozen=True)
class RepoBinding:
    """One executable symbol visible in the repository."""

    symbol: str
    shape: str
    dtype: str
    signature: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("symbol", self.symbol),
            ("shape", self.shape),
            ("dtype", self.dtype),
            ("signature", self.signature),
        ):
            if not value.strip():
                raise GroundingError(f"{field_name} must not be empty")


@dataclass(frozen=True)
class SymbolBinding:
    """A claimed variable mapped to a repository symbol."""

    variable: str
    repo_symbol: str
    expected_shape: str | None = None
    expected_dtype: str | None = None
    expected_signature: str | None = None

    def __post_init__(self) -> None:
        if not self.variable.strip() or not self.repo_symbol.strip():
            raise GroundingError("variable and repo_symbol must not be empty")


@dataclass(frozen=True)
class DomainClaim:
    """External knowledge that a planner wants to inject."""

    formula: str
    source: str
    bindings: tuple[SymbolBinding, ...]
    boundary_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.formula.strip():
            raise GroundingError("formula must not be empty")
        if not self.source.strip():
            raise GroundingError("source must not be empty")
        if not self.bindings:
            raise GroundingError("at least one executable binding is required")
        if not self.boundary_conditions:
            raise GroundingError("boundary conditions must not be empty")
        variables = [binding.variable for binding in self.bindings]
        if len(set(variables)) != len(variables):
            raise GroundingError("binding variables must be unique")


@dataclass(frozen=True)
class AlignmentReport:
    aligned: bool
    missing_symbols: tuple[str, ...]
    shape_mismatches: tuple[str, ...]
    dtype_mismatches: tuple[str, ...]
    signature_mismatches: tuple[str, ...]
    reason: str

    @property
    def anchors(self) -> int:
        return 0 if self.aligned else 1

    def to_dict(self) -> dict[str, object]:
        return {
            "aligned": self.aligned,
            "missing_symbols": list(self.missing_symbols),
            "shape_mismatches": list(self.shape_mismatches),
            "dtype_mismatches": list(self.dtype_mismatches),
            "signature_mismatches": list(self.signature_mismatches),
            "reason": self.reason,
        }


class ScientificGroundingGate:
    """Require every external claim to bind to the repository context."""

    def validate(
        self,
        claim: DomainClaim,
        repository: dict[str, RepoBinding],
    ) -> AlignmentReport:
        if not repository:
            raise GroundingError("repository symbol table must not be empty")
        missing: list[str] = []
        shape_mismatches: list[str] = []
        dtype_mismatches: list[str] = []
        signature_mismatches: list[str] = []
        for binding in claim.bindings:
            symbol = repository.get(binding.repo_symbol)
            if symbol is None:
                missing.append(binding.repo_symbol)
                continue
            if binding.expected_shape is not None and binding.expected_shape != symbol.shape:
                shape_mismatches.append(
                    f"{binding.repo_symbol}: {binding.expected_shape} != {symbol.shape}"
                )
            if binding.expected_dtype is not None and binding.expected_dtype != symbol.dtype:
                dtype_mismatches.append(
                    f"{binding.repo_symbol}: {binding.expected_dtype} != {symbol.dtype}"
                )
            if (
                binding.expected_signature is not None
                and binding.expected_signature != symbol.signature
            ):
                signature_mismatches.append(
                    f"{binding.repo_symbol}: {binding.expected_signature} != {symbol.signature}"
                )
        aligned = not (
            missing or shape_mismatches or dtype_mismatches or signature_mismatches
        )
        if aligned:
            reason = "every variable binds to an executable repository symbol"
        else:
            reason = "external claim does not fully bind to the repository context"
        return AlignmentReport(
            aligned=aligned,
            missing_symbols=tuple(missing),
            shape_mismatches=tuple(shape_mismatches),
            dtype_mismatches=tuple(dtype_mismatches),
            signature_mismatches=tuple(signature_mismatches),
            reason=reason,
        )

    def render(self, claim: DomainClaim) -> str:
        """Render only an already-validated claim; callers still call validate."""
        bindings = ", ".join(
            f"{binding.variable}->{binding.repo_symbol}" for binding in claim.bindings
        )
        boundaries = "; ".join(claim.boundary_conditions)
        return (
            f"## Grounded domain knowledge\n"
            f"Formula: {claim.formula.strip()}\n"
            f"Source: {claim.source.strip()}\n"
            f"Bindings: {bindings}\n"
            f"Boundary conditions: {boundaries}\n"
            "Use the formula only inside these executable bindings; do not "
            "generalise it beyond the stated boundary."
        )

    def gate(
        self,
        claim: DomainClaim,
        repository: dict[str, RepoBinding],
    ) -> str | None:
        """Return the prompt block only when the claim is aligned."""
        report = self.validate(claim, repository)
        if not report.aligned:
            return None
        return self.render(claim)
