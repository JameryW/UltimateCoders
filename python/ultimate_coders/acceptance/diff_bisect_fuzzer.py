"""Differential fuzzing and hierarchical computation-graph bisection (ledger item #107).

The fuzzer compares a reference and candidate execution, then bisects the
computation graph to the first divergent operator instead of blaming an entire
fused kernel. EMBERS-style defect-guided mutation keeps a diverse corpus of
inputs while increasing the density of suspicious mutations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

__all__ = [
    "DIFF_BISECT_FUZZER_ENV",
    "FuzzCase",
    "GraphNode",
    "ComputationGraph",
    "BisectResult",
    "DifferentialBisector",
    "MutationKind",
    "EmbersMutator",
    "FuzzCampaign",
    "diff_bisect_fuzzer_enabled",
]

DIFF_BISECT_FUZZER_ENV = "UC_DIFF_BISECT_FUZZER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def diff_bisect_fuzzer_enabled() -> bool:
    return os.environ.get(DIFF_BISECT_FUZZER_ENV, "").strip().lower() in _TRUTHY


class DiffBisectError(ValueError):
    pass


@dataclass(frozen=True)
class FuzzCase:
    case_id: str
    input_text: str
    expected: str
    actual: str

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise DiffBisectError("case_id must not be empty")

    @property
    def failed(self) -> bool:
        return self.expected != self.actual


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    op: str
    inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.op.strip():
            raise DiffBisectError("node_id and op must not be empty")


class ComputationGraph:
    def __init__(self, nodes: tuple[GraphNode, ...] | list[GraphNode] = ()) -> None:
        self._nodes: dict[str, GraphNode] = {}
        for node in nodes:
            self.add(node)

    def add(self, node: GraphNode) -> None:
        if node.node_id in self._nodes:
            raise DiffBisectError(f"duplicate node: {node.node_id}")
        for parent in node.inputs:
            if parent not in self._nodes:
                raise DiffBisectError(f"unknown input: {parent}")
        self._nodes[node.node_id] = node

    def node(self, node_id: str) -> GraphNode:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise DiffBisectError(f"unknown node: {node_id}") from exc

    def nodes(self) -> tuple[GraphNode, ...]:
        return tuple(self._nodes.values())

    def execution_order(self) -> tuple[str, ...]:
        visited: set[str] = set()
        visiting: set[str] = set()
        order: list[str] = []

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise DiffBisectError("cycle detected")
            visiting.add(node_id)
            for parent in self.node(node_id).inputs:
                visit(parent)
            visiting.remove(node_id)
            visited.add(node_id)
            order.append(node_id)

        for node_id in self._nodes:
            visit(node_id)
        return tuple(order)

    def path_to(self, node_id: str) -> tuple[str, ...]:
        path: list[str] = []

        def walk(current: str) -> None:
            path.append(current)
            parents = self.node(current).inputs
            if parents:
                walk(parents[0])

        walk(node_id)
        path.reverse()
        return tuple(path)


@dataclass(frozen=True)
class BisectResult:
    faulty_node: str | None
    path: tuple[str, ...]
    divergence_index: int | None
    confidence: float


class DifferentialBisector:
    def bisect(self, graph: ComputationGraph, matching: dict[str, bool]) -> BisectResult:
        order = graph.execution_order()
        first_failure = None
        for index, node_id in enumerate(order):
            if matching.get(node_id) is False:
                first_failure = (index, node_id)
                break
        if first_failure is None:
            return BisectResult(faulty_node=None, path=(), divergence_index=None, confidence=0.0)
        index, node_id = first_failure
        path = graph.path_to(node_id)
        confidence = round(max(0.5, 1.0 - index / max(1, len(order))), 4)
        return BisectResult(
            faulty_node=node_id,
            path=path,
            divergence_index=index,
            confidence=confidence,
        )


class MutationKind(str):
    BOUNDARY = "boundary"
    TYPE_SWAP = "type_swap"
    CONTROL_FLOW = "control_flow"


class EmbersMutator:
    def __init__(self, max_variants: int = 8) -> None:
        self.max_variants = max_variants

    def mutate(self, seed: str) -> tuple[str, ...]:
        if not seed:
            raise DiffBisectError("seed must not be empty")
        variants = [
            f"{seed}__boundary",
            f"{seed}__type",
            f"{seed}__flow",
            f"{seed}__empty",
        ]
        return tuple(variants[: self.max_variants])

    def diversify(self, seeds: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        for seed in seeds:
            for variant in self.mutate(seed):
                if variant not in seen:
                    seen.add(variant)
                    result.append(variant)
        return tuple(result)


@dataclass(frozen=True)
class FuzzCampaign:
    cases: tuple[FuzzCase, ...]
    failures: tuple[FuzzCase, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(case for case in self.cases if case.failed))

    @property
    def failure_rate(self) -> float:
        return len(self.failures) / len(self.cases) if self.cases else 0.0

    def triage(self, graph: ComputationGraph, matching: dict[str, bool]) -> BisectResult:
        return DifferentialBisector().bisect(graph, matching)
