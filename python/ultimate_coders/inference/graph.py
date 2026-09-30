"""Execution/adaptation relationships, distinct from UC's scheduling DAG."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .models import MetaInferTask


class AdaptationNodeKind(str, Enum):
    MODEL_SEMANTIC = "model_semantic"
    RUNTIME = "runtime"
    DISPATCH = "dispatch"
    KERNEL = "kernel"
    HARDWARE = "hardware"
    EVIDENCE = "evidence"


@dataclass(frozen=True)
class AdaptationNode:
    id: str
    kind: AdaptationNodeKind
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.label:
            raise ValueError("Graph node id and label are required")
        if not isinstance(self.kind, AdaptationNodeKind):
            object.__setattr__(self, "kind", AdaptationNodeKind(self.kind))


@dataclass(frozen=True)
class AdaptationEdge:
    source: str
    target: str
    relation: str


@dataclass
class ExecutionAdaptationGraph:
    nodes: list[AdaptationNode] = field(default_factory=list)
    edges: list[AdaptationEdge] = field(default_factory=list)

    def validate(self) -> None:
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("Duplicate adaptation graph node")
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids or not edge.relation:
                raise ValueError("Invalid adaptation graph edge")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "version": 1,
            "nodes": [{**asdict(node), "kind": node.kind.value} for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionAdaptationGraph:
        if data.get("version") != 1:
            raise ValueError("Unsupported adaptation graph version")
        graph = cls(
            [AdaptationNode(**node) for node in data["nodes"]],
            [AdaptationEdge(**edge) for edge in data["edges"]],
        )
        graph.validate()
        return graph

    @classmethod
    def for_task(cls, task: MetaInferTask) -> ExecutionAdaptationGraph:
        # These are declared task relationships, not invented discoveries.
        nodes = [
            AdaptationNode(
                "runtime",
                AdaptationNodeKind.RUNTIME,
                task.framework or task.repository,
                {"provenance": "task"},
            ),
            AdaptationNode(
                "dispatch",
                AdaptationNodeKind.DISPATCH,
                task.task_type.value,
                {"provenance": "task"},
            ),
        ]
        edges = [AdaptationEdge("runtime", "dispatch", "requests")]
        for key, label, kind, source in (
            ("model", task.model, AdaptationNodeKind.MODEL_SEMANTIC, "runtime"),
            ("hardware", task.hardware, AdaptationNodeKind.HARDWARE, "dispatch"),
            (
                "kernel",
                task.parameters.get("kernel_file_path"),
                AdaptationNodeKind.KERNEL,
                "dispatch",
            ),
        ):
            if label:
                nodes.append(AdaptationNode(key, kind, str(label), {"provenance": "task"}))
                edges.append(AdaptationEdge(source, key, "targets"))
        return cls(nodes, edges)

    def record_evidence(self, index: int, evidence: dict[str, Any]) -> None:
        node_id = f"experiment-{index}"
        self.nodes.append(
            AdaptationNode(node_id, AdaptationNodeKind.EVIDENCE, f"Experiment {index}", evidence)
        )
        self.edges.append(AdaptationEdge("dispatch", node_id, "verified_by"))
