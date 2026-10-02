"""Reusable correctness, performance and memory acceptance gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import METRIC_DIRECTIONS, BenchmarkResult, MetaInferTask, nonnegative_number


@dataclass(frozen=True)
class OraclePolicy:
    objective: str = "tpot_ms"
    min_improvement_pct: float = 0.0
    max_regression_pct: float = 0.0
    max_memory_gb: float | None = None
    max_numerical_error: float | None = None
    max_dispersion_pct: float = 5.0
    noise_multiplier: float = 2.0

    @classmethod
    def for_task(cls, task: MetaInferTask, overrides: dict[str, Any] | None = None) -> OraclePolicy:
        settings = dict(overrides or {})
        if "objective" not in settings:
            objective = task.objective.lower()
            for token, metric in (
                ("throughput", "throughput_tokens_s"),
                ("ttft", "ttft_ms"),
                ("tpot", "tpot_ms"),
                ("memory", "peak_memory_gb"),
                ("latency", "latency_ms"),
            ):
                if token in objective:
                    settings["objective"] = metric
                    break
        for constraint, field_name in (
            ("memory_gb", "max_memory_gb"),
            ("numerical_error", "max_numerical_error"),
        ):
            if constraint in task.constraints:
                limit = nonnegative_number(task.constraints[constraint], constraint)
                override = settings.get(field_name)
                settings[field_name] = min(limit, override) if override is not None else limit
        return cls(**settings)

    def __post_init__(self) -> None:
        if self.objective not in METRIC_DIRECTIONS:
            raise ValueError(f"Unknown objective metric {self.objective}")
        for name, value in asdict(self).items():
            if name != "objective" and value is not None:
                nonnegative_number(value, name)


@dataclass(frozen=True)
class OracleVerdict:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    improvement_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Oracle:
    def __init__(self, policy: OraclePolicy | None = None) -> None:
        self.policy = policy or OraclePolicy()

    def validate(self, result: BenchmarkResult) -> list[str]:
        # Revalidate mutable metric maps at the trust boundary.
        BenchmarkResult.from_dict(result.to_dict())
        reasons = []
        count = result.statistics.get("count", 0)
        if type(count) is not int or count < 3:
            reasons.append("At least three benchmark samples are required")
        if not result.environment_id:
            reasons.append("Benchmark environment identity is required")
        if set(result.statistics.get("dispersion_pct", {})) != set(result.metrics):
            reasons.append("Dispersion evidence is missing for measured metrics")
        if not result.compile_success:
            reasons.append("Compilation failed")
        if not result.correctness:
            reasons.append("Correctness failed")
        if self.policy.objective not in result.metrics:
            reasons.append(f"Missing objective measurement {self.policy.objective}")
        for metric, dispersion in result.statistics.get("dispersion_pct", {}).items():
            nonnegative_number(dispersion, "dispersion_pct")
            if dispersion > self.policy.max_dispersion_pct:
                reasons.append(f"Unstable benchmark measurement {metric}")
        if self.policy.max_memory_gb is not None:
            memory = result.metrics.get("peak_memory_gb")
            if memory is None or memory > self.policy.max_memory_gb:
                reasons.append("Missing or excessive peak_memory_gb")
        if self.policy.max_numerical_error is not None:
            if (
                result.numerical_error is None
                or result.numerical_error > self.policy.max_numerical_error
            ):
                reasons.append("Missing or excessive numerical_error")
        return reasons

    def evaluate(self, baseline: BenchmarkResult, candidate: BenchmarkResult) -> OracleVerdict:
        reasons = [f"Baseline: {r}" for r in self.validate(baseline)]
        reasons.extend(self.validate(candidate))
        if candidate.workload_id != baseline.workload_id:
            reasons.append("Workload identities differ")
        if candidate.environment_id != baseline.environment_id:
            reasons.append("Benchmark environments differ")
        improvement = None
        for metric, before in baseline.metrics.items():
            after = candidate.metrics.get(metric)
            if after is None:
                reasons.append(f"Missing candidate measurement {metric}")
                continue
            delta = METRIC_DIRECTIONS[metric] * (after - before)
            # Zero baseline has no meaningful percentage; equality cannot improve it.
            pct = 100 * delta / before if before > 0 else (0 if delta == 0 else None)
            if delta < 0 and (pct is None or pct < -self.policy.max_regression_pct):
                reasons.append(f"Performance regression in {metric}: {before} -> {after}")
            if metric == self.policy.objective:
                improvement = pct
                noise = self.policy.noise_multiplier * sum(
                    result.statistics.get("dispersion_pct", {}).get(metric, 0)
                    for result in (baseline, candidate)
                )
                if pct is not None and pct <= noise:
                    reasons.append("Improvement does not exceed measurement noise")
                if delta <= 0 or (pct is not None and pct < self.policy.min_improvement_pct):
                    reasons.append(f"No sufficient improvement in {metric}")
        return OracleVerdict(not reasons, reasons, improvement)
