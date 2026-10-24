"""Optional inference infrastructure domain and shared experiment primitives."""

from .adapter import MetaInferAdapter, MetaInferError
from .agent import InferenceInfraAgent
from .benchmark import BenchmarkRunner, BenchmarkSpec
from .graph import AdaptationEdge, AdaptationNode, AdaptationNodeKind, ExecutionAdaptationGraph
from .models import (
    BenchmarkResult,
    EvalRole,
    InferenceTaskType,
    MetaInferResult,
    MetaInferTask,
    assert_report_identity,
    mint_report_result,
    published_uplift_verdict,
    report_uplift_enforce_enabled,
    require_report_role,
)
from .oracle import Oracle, OraclePolicy, OracleVerdict
from .workflow import OptimizationWorkflow

__all__ = [
    "BenchmarkResult",
    "EvalRole",
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
    "assert_report_identity",
    "mint_report_result",
    "published_uplift_verdict",
    "report_uplift_enforce_enabled",
    "require_report_role",
]
