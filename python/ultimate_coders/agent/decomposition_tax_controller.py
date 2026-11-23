"""Decomposition-tax controller for multi-stage coding pipelines.

Implements ledger item #56 (The Decomposition Tax, arXiv:2609.32825 and the
break-even analysis in arXiv:2610.37700). A pipeline is only worth its
interfaces when the decomposition expansion factor stays below the inverse
price ratio. Every lossy hand-off is followed by a mandatory re-anchoring of the
original issue; the controller never rewrites the issue itself.

The module is opt-in via ``UC_DECOMPOSITION_TAX_CONTROLLER`` and performs no
model calls. It is deterministic bookkeeping over caller-supplied stage sizes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "DECOMPOSITION_TAX_CONTROLLER_ENV",
    "DecompositionError",
    "DecompositionTaxController",
    "PipelinePlan",
    "Regrounding",
    "RoutingDecision",
    "StageSpec",
    "decomposition_tax_enabled",
    "reground_after_lossy_stage",
]

DECOMPOSITION_TAX_CONTROLLER_ENV = "UC_DECOMPOSITION_TAX_CONTROLLER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def decomposition_tax_enabled() -> bool:
    """Return whether the controller is enabled in this environment."""
    import os

    return os.environ.get(DECOMPOSITION_TAX_CONTROLLER_ENV, "").strip().lower() in _TRUTHY


class DecompositionError(ValueError):
    """A malformed stage or pipeline specification."""


@dataclass(frozen=True)
class StageSpec:
    """One pipeline stage and its output size in tokens."""

    name: str
    output_tokens: int
    condensing: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DecompositionError("stage name must not be empty")
        if self.output_tokens <= 0:
            raise DecompositionError("stage output_tokens must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "output_tokens": self.output_tokens,
            "condensing": self.condensing,
        }


@dataclass(frozen=True)
class PipelinePlan:
    """An ordered pipeline. The first stage defines the reference size O_1."""

    stages: tuple[StageSpec, ...]

    def __post_init__(self) -> None:
        if not self.stages:
            raise DecompositionError("pipeline must contain at least one stage")
        names = [stage.name for stage in self.stages]
        if len(set(names)) != len(names):
            raise DecompositionError("stage names must be unique")

    @property
    def reference_tokens(self) -> int:
        return self.stages[0].output_tokens

    @property
    def expansion_factor(self) -> float:
        """Return ``alpha = (sum o_i) / O_1``."""
        total = sum(stage.output_tokens for stage in self.stages)
        return total / self.reference_tokens

    @property
    def lossy_stage_indices(self) -> tuple[int, ...]:
        return tuple(
            index
            for index, stage in enumerate(self.stages)
            if stage.condensing and index > 0
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "stages": [stage.to_dict() for stage in self.stages],
            "reference_tokens": self.reference_tokens,
            "expansion_factor": self.expansion_factor,
            "lossy_stage_indices": list(self.lossy_stage_indices),
        }


@dataclass(frozen=True)
class RoutingDecision:
    """The break-even routing decision for one pipeline."""

    route: str
    alpha: float
    price_ratio: float
    break_even_alpha: float
    reason: str

    @property
    def uses_pipeline(self) -> bool:
        return self.route == "pipeline"

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "alpha": self.alpha,
            "price_ratio": self.price_ratio,
            "break_even_alpha": self.break_even_alpha,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Regrounding:
    """Original-issue re-anchoring injected after a lossy stage."""

    original_issue: str
    downstream_input: str
    lossy_stage: str
    preserve_relations: bool = True

    def __post_init__(self) -> None:
        if not self.original_issue.strip():
            raise DecompositionError("original_issue must not be empty")
        if not self.lossy_stage.strip():
            raise DecompositionError("lossy_stage must not be empty")

    def render(self) -> str:
        """Render the bounded re-anchoring block."""
        relation_clause = (
            "Preserve every entity, number, identifier, and relation between them."
            if self.preserve_relations
            else "Preserve every entity and identifier."
        )
        downstream = self.downstream_input.strip() or "(empty downstream input)"
        return (
            f"## Original issue (re-anchored after lossy stage: {self.lossy_stage})\n"
            f"{self.original_issue.strip()}\n\n"
            f"## Current stage input\n"
            f"{downstream}\n\n"
            f"{relation_clause}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "original_issue": self.original_issue,
            "downstream_input": self.downstream_input,
            "lossy_stage": self.lossy_stage,
            "preserve_relations": self.preserve_relations,
        }


class DecompositionTaxController:
    """Choose pipeline vs. single-call routing at the break-even boundary."""

    def __init__(self, price_ratio: float = 0.25) -> None:
        if not isinstance(price_ratio, (int, float)) or price_ratio <= 0:
            raise DecompositionError("price_ratio must be positive")
        self._price_ratio = float(price_ratio)

    @property
    def price_ratio(self) -> float:
        return self._price_ratio

    @property
    def break_even_alpha(self) -> float:
        return 1.0 / self._price_ratio

    def decide(self, plan: PipelinePlan) -> RoutingDecision:
        """Route a plan, treating equality as a failed break-even test."""
        alpha = plan.expansion_factor
        if alpha < self.break_even_alpha:
            return RoutingDecision(
                route="pipeline",
                alpha=alpha,
                price_ratio=self._price_ratio,
                break_even_alpha=self.break_even_alpha,
                reason="expansion factor is below the inverse price ratio",
            )
        return RoutingDecision(
            route="single_call",
            alpha=alpha,
            price_ratio=self._price_ratio,
            break_even_alpha=self.break_even_alpha,
            reason="expansion factor meets or exceeds the inverse price ratio",
        )

    def reground(
        self,
        plan: PipelinePlan,
        stage_index: int,
        original_issue: str,
        downstream_input: str,
    ) -> Regrounding | None:
        """Return re-anchoring for a lossy hand-off, or ``None`` if not needed."""
        if stage_index < 0 or stage_index >= len(plan.stages):
            raise DecompositionError("stage_index is out of range")
        stage = plan.stages[stage_index]
        if not stage.condensing or stage_index == 0:
            return None
        return Regrounding(
            original_issue=original_issue,
            downstream_input=downstream_input,
            lossy_stage=stage.name,
        )

    def reground_many(
        self,
        plan: PipelinePlan,
        original_issue: str,
        downstream_inputs: Iterable[str],
    ) -> tuple[Regrounding, ...]:
        """Return re-anchoring for every lossy hand-off in the plan."""
        inputs = tuple(downstream_inputs)
        if len(inputs) != len(plan.lossy_stage_indices):
            raise DecompositionError(
                "downstream_inputs must match the number of lossy stages"
            )
        blocks: list[Regrounding] = []
        for stage_index, downstream in zip(plan.lossy_stage_indices, inputs):
            block = self.reground(
                plan,
                stage_index,
                original_issue,
                downstream,
            )
            if block is not None:
                blocks.append(block)
        return tuple(blocks)


def reground_after_lossy_stage(
    original_issue: str,
    downstream_input: str,
    lossy_stage: str,
    *,
    preserve_relations: bool = True,
) -> str:
    """Convenience wrapper used by a pipeline node."""
    return Regrounding(
        original_issue=original_issue,
        downstream_input=downstream_input,
        lossy_stage=lossy_stage,
        preserve_relations=preserve_relations,
    ).render()
