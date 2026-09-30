"""Optional inference infrastructure domain and shared experiment primitives."""

from .adapter import MetaInferAdapter, MetaInferError
from .agent import InferenceInfraAgent
from .benchmark import BenchmarkRunner, BenchmarkSpec
from .graph import AdaptationEdge, AdaptationNode, AdaptationNodeKind, ExecutionAdaptationGraph
from .models import BenchmarkResult, InferenceTaskType, MetaInferResult, MetaInferTask
from .oracle import Oracle, OraclePolicy, OracleVerdict
from .workflow import OptimizationWorkflow

__all__ = [
    "BenchmarkResult",
    "InferenceTaskType",
    "MetaInferResult",
    "MetaInferTask",
    "Oracle",
    "OraclePolicy",
    "OracleVerdict",
    "MetaInferAdapter",
    "MetaInferError",
    "InferenceInfraAgent",
    "BenchmarkRunner",
    "BenchmarkSpec",
    "AdaptationEdge",
    "AdaptationNode",
    "AdaptationNodeKind",
    "ExecutionAdaptationGraph",
    "OptimizationWorkflow",
]
