"""Portable inference contracts; no GPU or MetaInfer dependency."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

METRIC_DIRECTIONS = {
    "latency_ms": -1,
    "ttft_ms": -1,
    "tpot_ms": -1,
    "throughput_tokens_s": 1,
    "peak_memory_gb": -1,
}


def nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return float(value)


class InferenceTaskType(str, Enum):
    PORT_MODEL = "port_model"
    OPTIMIZE_KERNEL = "optimize_kernel"
    OPTIMIZE_RUNTIME = "optimize_runtime"
    ANALYZE_TRACE = "analyze_trace"
    BENCHMARK = "benchmark"


@dataclass(frozen=True)
class BenchmarkResult:
    workload_id: str
    correctness: bool
    compile_success: bool
    metrics: dict[str, float] = field(default_factory=dict)
    numerical_error: float | None = None
    evidence: list[str] = field(default_factory=list)
    profile: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workload_id, str) or not self.workload_id.strip():
            raise ValueError("workload_id is required")
        if type(self.correctness) is not bool or type(self.compile_success) is not bool:
            raise ValueError("correctness and compile_success must be booleans")
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
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
