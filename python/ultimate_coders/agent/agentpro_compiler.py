"""AgentPRO-style workflow compiler (ledger item #109, UC_AGENTPRO_COMPILER).

Pure, deterministic planning helper for the agent layer. It builds a
layered IR from workflow operations, fuses adjacent LLM calls that do not
need an intermediate observation, removes redundant reads, and attaches a
guard/fallback path to each compiled region. It never calls a model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

AGENTPRO_COMPILER_ENV = "UC_AGENTPRO_COMPILER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_VALID_KINDS = frozenset({"llm", "tool", "read"})


def agentpro_compiler_enabled() -> bool:
    return os.environ.get(AGENTPRO_COMPILER_ENV, "").strip().lower() in _TRUTHY


class AgentProError(ValueError):
    """Raised when a workflow IR is malformed."""


@dataclass(frozen=True)
class WorkflowOp:
    id: str
    kind: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    token_cost: int = 100
    observes_previous: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise AgentProError("op id must not be empty")
        if self.kind not in _VALID_KINDS:
            raise AgentProError(f"unknown op kind: {self.kind}")
        if self.token_cost < 0:
            raise AgentProError("token_cost must not be negative")


@dataclass(frozen=True)
class CompiledRegion:
    op_ids: tuple[str, ...]
    fused: bool
    guard: str
    eliminated_reads: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "op_ids": list(self.op_ids),
            "fused": self.fused,
            "guard": self.guard,
            "eliminated_reads": list(self.eliminated_reads),
        }


@dataclass(frozen=True)
class CompiledWorkflow:
    regions: tuple[CompiledRegion, ...]
    removed_reads: tuple[str, ...]
    original_tokens: int
    compiled_tokens: int

    @property
    def token_saving_ratio(self) -> float:
        if self.original_tokens == 0:
            return 0.0
        return round(1.0 - self.compiled_tokens / self.original_tokens, 4)

    @property
    def fallback_path(self) -> tuple[str, ...]:
        return tuple(region.guard for region in self.regions)

    def to_dict(self) -> dict[str, object]:
        return {
            "regions": [region.to_dict() for region in self.regions],
            "removed_reads": list(self.removed_reads),
            "original_tokens": self.original_tokens,
            "compiled_tokens": self.compiled_tokens,
            "token_saving_ratio": self.token_saving_ratio,
            "fallback_path": list(self.fallback_path),
        }


class AgentProCompiler:
    """Compile a flat workflow into fused regions with guards."""

    def compile(self, ops: tuple[WorkflowOp, ...] | list[WorkflowOp]) -> CompiledWorkflow:
        if not ops:
            raise AgentProError("workflow must not be empty")
        ids = [op.id for op in ops]
        if len(set(ids)) != len(ids):
            raise AgentProError("duplicate op id")

        available: set[str] = set()
        removed: list[str] = []
        eliminated_by_op: dict[str, tuple[str, ...]] = {}
        for op in ops:
            eliminated = []
            for read in op.reads:
                if read in available:
                    eliminated.append(read)
            removed.extend(eliminated)
            eliminated_by_op[op.id] = tuple(eliminated)
            available.update(op.writes)

        regions: list[CompiledRegion] = []
        i = 0
        while i < len(ops):
            op = ops[i]
            if op.kind == "llm" and not op.observes_previous:
                group = [op]
                j = i + 1
                while j < len(ops):
                    nxt = ops[j]
                    if nxt.kind == "llm" and not nxt.observes_previous:
                        group.append(nxt)
                        j += 1
                    else:
                        break
                if len(group) > 1:
                    eliminated = tuple(
                        read for member in group for read in eliminated_by_op[member.id]
                    )
                    regions.append(
                        CompiledRegion(
                            op_ids=tuple(member.id for member in group),
                            fused=True,
                            guard=f"guard:{group[0].id}->fallback:{group[-1].id}",
                            eliminated_reads=eliminated,
                        )
                    )
                    i = j
                    continue
            regions.append(
                CompiledRegion(
                    op_ids=(op.id,),
                    fused=False,
                    guard=f"guard:{op.id}",
                    eliminated_reads=eliminated_by_op[op.id],
                )
            )
            i += 1

        original = sum(op.token_cost for op in ops)
        saved_reads = len(removed) * 10
        costs = {op.id: op.token_cost for op in ops}
        fused_savings = sum(
            int(sum(costs[op_id] for op_id in region.op_ids) * 0.3)
            for region in regions
            if region.fused
        )
        compiled = max(0, original - saved_reads - fused_savings)
        return CompiledWorkflow(
            regions=tuple(regions),
            removed_reads=tuple(removed),
            original_tokens=original,
            compiled_tokens=compiled,
        )


__all__ = [
    "AGENTPRO_COMPILER_ENV",
    "AgentProCompiler",
    "AgentProError",
    "CompiledRegion",
    "CompiledWorkflow",
    "WorkflowOp",
    "agentpro_compiler_enabled",
]
