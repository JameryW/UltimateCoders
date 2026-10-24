"""Portable inference contracts; no GPU or MetaInfer dependency."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any

METRIC_DIRECTIONS = {
    "latency_ms": -1,
    "ttft_ms": -1,
    "tpot_ms": -1,
    "throughput_tokens_s": 1,
    "peak_memory_gb": -1,
}

EVAL_ROLE_ENFORCE_ENV = "UC_EVAL_ROLE_ENFORCE"
REPORT_UPLIFT_ENFORCE_ENV = "UC_REPORT_UPLIFT_ENFORCE"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


def nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return float(value)


def _env_flag_enabled(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY_ENV


def eval_role_enforce_enabled() -> bool:
    """Identity enforcement for report-only consumers; default off."""
    return _env_flag_enabled(EVAL_ROLE_ENFORCE_ENV)


def report_uplift_enforce_enabled() -> bool:
    """Require report remasure for public uplift; default off."""
    return _env_flag_enabled(REPORT_UPLIFT_ENFORCE_ENV)


class InferenceTaskType(str, Enum):
    PORT_MODEL = "port_model"
    OPTIMIZE_KERNEL = "optimize_kernel"
    OPTIMIZE_RUNTIME = "optimize_runtime"
    ANALYZE_TRACE = "analyze_trace"
    BENCHMARK = "benchmark"


class EvalRole(str, Enum):
    """Benchmark / verdict identity: search feedback, select accept, report uplift."""

    SEARCH = "search"
    SELECT = "select"
    REPORT = "report"


def coerce_eval_role(value: EvalRole | str | None) -> EvalRole:
    if value is None:
        return EvalRole.SELECT
    if isinstance(value, EvalRole):
        return value
    return EvalRole(value)


def require_report_role(result_or_verdict: Any) -> None:
    """Reject non-report identities when eval-role or report-uplift enforce is on.

    ``UC_REPORT_UPLIFT_ENFORCE`` implies this check at publish boundaries even when
    ``UC_EVAL_ROLE_ENFORCE`` is off. Dicts with an ``eval_role`` key are accepted.
    """
    if not (eval_role_enforce_enabled() or report_uplift_enforce_enabled()):
        return
    if isinstance(result_or_verdict, dict):
        role = result_or_verdict.get("eval_role")
    else:
        role = getattr(result_or_verdict, "eval_role", None)
    if coerce_eval_role(role) is not EvalRole.REPORT:
        raise ValueError(
            f"report identity required; got eval_role={coerce_eval_role(role).value}"
        )


def assert_report_identity(result_or_verdict: Any) -> None:
    """Alias for :func:`require_report_role` (ticket 02 uplift consumers)."""
    require_report_role(result_or_verdict)


def published_uplift_verdict(result: dict[str, Any]) -> dict[str, Any] | None:
    """Verdict used for public publication (manifest / report uplift figures).

    When ``UC_REPORT_UPLIFT_ENFORCE`` is on, only ``report_verdict`` may populate
    public uplift; select/search iteration verdicts are never a fallback. Missing
    remasure on a successful optimization accept (iterations present) fails closed.
    Non-optimization successes (analyze/benchmark) publish no uplift verdict.
    """
    if report_uplift_enforce_enabled():
        report = result.get("report_verdict")
        if isinstance(report, dict):
            require_report_role(report)
            return report
        # Legacy publish path used the last iteration verdict. Under enforce that
        # select/search score must never become the public uplift figure.
        iterations = result.get("iterations") or []
        if result.get("success") and iterations:
            raise ValueError(
                "report remasure required for publication under "
                f"{REPORT_UPLIFT_ENFORCE_ENV}"
            )
        return None
    iterations = result.get("iterations") or []
    if not iterations:
        return None
    verdict = iterations[-1].get("verdict")
    return verdict if isinstance(verdict, dict) else None


def refuse_relabel_to_report(current: EvalRole, new_role: EvalRole) -> None:
    """When enforce is on, search/select may not be relabeled as report."""
    if not eval_role_enforce_enabled():
        return
    if current is not EvalRole.REPORT and new_role is EvalRole.REPORT:
        raise ValueError(
            f"cannot relabel {current.value} as report under {EVAL_ROLE_ENFORCE_ENV}"
        )


@dataclass(frozen=True)
class BenchmarkResult:
    workload_id: str
    correctness: bool
    compile_success: bool
    metrics: dict[str, float] = field(default_factory=dict)
    numerical_error: float | None = None
    evidence: list[str] = field(default_factory=list)
    profile: str | None = None
    statistics: dict[str, Any] = field(default_factory=dict)
    environment_id: str = ""
    eval_role: EvalRole = EvalRole.SELECT

    def __post_init__(self) -> None:
        if not isinstance(self.workload_id, str) or not self.workload_id.strip():
            raise ValueError("workload_id is required")
        if type(self.correctness) is not bool or type(self.compile_success) is not bool:
            raise ValueError("correctness and compile_success must be booleans")
        object.__setattr__(self, "eval_role", coerce_eval_role(self.eval_role))
        for name, value in self.metrics.items():
            if name not in METRIC_DIRECTIONS:
                raise ValueError(f"Unknown metric {name}")
            nonnegative_number(value, name)
        if self.numerical_error is not None:
            nonnegative_number(self.numerical_error, "numerical_error")
        if not isinstance(self.evidence, list) or any(
            not isinstance(item, str) for item in self.evidence
        ):
            raise ValueError("evidence must be a list of strings")
        if self.profile is not None and not isinstance(self.profile, str):
            raise ValueError("profile must be text")

    def with_role(self, role: EvalRole | str) -> BenchmarkResult:
        new_role = coerce_eval_role(role)
        refuse_relabel_to_report(self.eval_role, new_role)
        return replace(self, eval_role=new_role)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkResult:
        return cls(
            workload_id=data["workload_id"],
            correctness=data["correctness"],
            compile_success=data["compile_success"],
            metrics=data.get("metrics", {}),
            numerical_error=data.get("numerical_error"),
            evidence=data.get("evidence", []),
            profile=data.get("profile"),
            statistics=data.get("statistics", {}),
            environment_id=data.get("environment_id", ""),
            eval_role=coerce_eval_role(data.get("eval_role")),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["eval_role"] = self.eval_role.value
        return data


def mint_report_result(result: BenchmarkResult) -> BenchmarkResult:
    """Mint a report-role result via explicit construction (not ``with_role`` relabel)."""
    data = result.to_dict()
    data["eval_role"] = EvalRole.REPORT.value
    return BenchmarkResult.from_dict(data)


@dataclass(frozen=True)
class MetaInferTask:
    task_type: InferenceTaskType
    repository: str
    objective: str
    framework: str = ""
    model: str = ""
    hardware: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    upstream_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_type, InferenceTaskType):
            object.__setattr__(self, "task_type", InferenceTaskType(self.task_type))
        if not self.repository or not self.objective:
            raise ValueError("repository and objective are required")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetaInferTask:
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "task_type": self.task_type.value}


@dataclass
class MetaInferResult:
    task_id: str
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    patch: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
