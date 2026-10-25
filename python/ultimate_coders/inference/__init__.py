"""Optional inference infrastructure domain and shared experiment primitives."""

from .adapter import MetaInferAdapter, MetaInferError
from .agent import InferenceInfraAgent
from .approx_eval import (
    APPROX_EVAL_ARMED_ENV,
    APPROX_EVAL_ENFORCE_ENV,
    DEFAULT_SPEARMAN_THRESHOLD,
    RankAgreementOutcome,
    approx_eval_armed,
    approx_eval_enforce_enabled,
    approx_eval_ready_to_prune,
    arm_approx_eval,
    disarm_approx_eval,
    evaluate_rank_agreement,
)
from .benchmark import BenchmarkRunner, BenchmarkSpec, IndependentReevalOutcome
from .graph import AdaptationEdge, AdaptationNode, AdaptationNodeKind, ExecutionAdaptationGraph
from .models import (
    BenchmarkResult,
    EvalRole,
    InferenceTaskType,
    MetaInferResult,
    MetaInferTask,
    assert_report_identity,
    dual_gate_enforce_enabled,
    independent_reeval_enforce_enabled,
    mint_report_result,
    published_uplift_verdict,
    report_uplift_enforce_enabled,
    require_report_role,
)
from .oracle import Oracle, OraclePolicy, OracleVerdict, apply_dual_gate
from .workflow import OptimizationWorkflow

__all__ = [
    "APPROX_EVAL_ARMED_ENV",
    "APPROX_EVAL_ENFORCE_ENV",
    "DEFAULT_SPEARMAN_THRESHOLD",
    "BenchmarkResult",
    "EvalRole",
    "InferenceTaskType",
    "MetaInferResult",
    "MetaInferTask",
    "Oracle",
    "OraclePolicy",
    "OracleVerdict",
    "RankAgreementOutcome",
    "MetaInferAdapter",
    "MetaInferError",
    "InferenceInfraAgent",
    "BenchmarkRunner",
    "BenchmarkSpec",
    "IndependentReevalOutcome",
    "AdaptationEdge",
    "AdaptationNode",
    "AdaptationNodeKind",
    "ExecutionAdaptationGraph",
    "OptimizationWorkflow",
    "apply_dual_gate",
    "approx_eval_armed",
    "approx_eval_enforce_enabled",
    "approx_eval_ready_to_prune",
    "arm_approx_eval",
    "assert_report_identity",
    "disarm_approx_eval",
    "dual_gate_enforce_enabled",
    "evaluate_rank_agreement",
    "independent_reeval_enforce_enabled",
    "mint_report_result",
    "published_uplift_verdict",
    "report_uplift_enforce_enabled",
    "require_report_role",
]
