"""ServerBench and CompressionBench accounting (V28 item #131).

ServerBench scores candidate serving strategies by qualified tokens per GPU
dollar under a latency budget.  CompressionBench compares a local proxy
ranking (perplexity) with the end-to-end model ranking, so a layer-wise proxy
cannot silently hide a downstream regression.

Both classes are pure bookkeeping: callers supply measurements.  The module
never starts a server, evaluates a model, or allocates a GPU.
``UC_SERVER_COMPRESSION_BENCH`` is the opt-in flag.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "SERVER_COMPRESSION_BENCH_ENV",
    "ServingVariant",
    "ServerScore",
    "ServerBenchReport",
    "ServerBench",
    "CompressionCandidate",
    "CompressionReport",
    "CompressionBench",
    "server_compression_bench_enabled",
]

SERVER_COMPRESSION_BENCH_ENV = "UC_SERVER_COMPRESSION_BENCH"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def server_compression_bench_enabled() -> bool:
    """Return whether the serving/compression benchmark is enabled."""

    return os.environ.get(SERVER_COMPRESSION_BENCH_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ServingVariant:
    """One measured serving strategy."""

    name: str
    qualified_tokens_per_gpu_dollar: float
    p95_latency_ms: float
    independent_verified: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("variant name must not be empty")
        if self.qualified_tokens_per_gpu_dollar <= 0:
            raise ValueError("qualified tokens per GPU dollar must be positive")
        if self.p95_latency_ms <= 0:
            raise ValueError("p95 latency must be positive")


@dataclass(frozen=True)
class ServerScore:
    """One variant's score against the baseline and budget."""

    name: str
    value_ratio: float
    latency_ok: bool
    verified: bool
    accepted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value_ratio": round(self.value_ratio, 4),
            "latency_ok": self.latency_ok,
            "verified": self.verified,
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class ServerBenchReport:
    """The best accepted variant and all scores."""

    baseline_name: str
    best: ServerScore | None
    scores: tuple[ServerScore, ...]

    @property
    def accepted_count(self) -> int:
        return sum(1 for s in self.scores if s.accepted)

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline_name,
            "best": self.best.to_dict() if self.best else None,
            "accepted_count": self.accepted_count,
            "scores": [s.to_dict() for s in self.scores],
        }


class ServerBench:
    """Score serving variants by per-dollar value under a latency budget."""

    def evaluate(
        self,
        variants: Iterable[ServingVariant],
        *,
        baseline_name: str,
        latency_budget_ms: float,
        min_value_ratio: float = 1.0,
    ) -> ServerBenchReport:
        if latency_budget_ms <= 0:
            raise ValueError("latency budget must be positive")
        if min_value_ratio < 0:
            raise ValueError("min_value_ratio must not be negative")
        ordered = tuple(variants)
        if not ordered:
            raise ValueError("at least one serving variant is required")
        names = [v.name for v in ordered]
        if len(set(names)) != len(names):
            raise ValueError("duplicate serving variant")
        baseline = next((v for v in ordered if v.name == baseline_name), None)
        if baseline is None:
            raise ValueError("baseline variant not found")

        scores: list[ServerScore] = []
        for variant in ordered:
            ratio = variant.qualified_tokens_per_gpu_dollar / (
                baseline.qualified_tokens_per_gpu_dollar
            )
            latency_ok = variant.p95_latency_ms <= latency_budget_ms
            accepted = (
                variant.independent_verified and latency_ok and ratio >= min_value_ratio
            )
            scores.append(
                ServerScore(
                    name=variant.name,
                    value_ratio=ratio,
                    latency_ok=latency_ok,
                    verified=variant.independent_verified,
                    accepted=accepted,
                )
            )
        accepted = [s for s in scores if s.accepted]
        best = max(accepted, key=lambda s: (s.value_ratio, s.name)) if accepted else None
        return ServerBenchReport(baseline_name, best, tuple(scores))


@dataclass(frozen=True)
class CompressionCandidate:
    """One compression configuration with proxy and end-to-end measurements."""

    name: str
    perplexity: float
    downstream_accuracy: float
    independent_verified: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("candidate name must not be empty")
        if self.perplexity <= 0:
            raise ValueError("perplexity must be positive")
        if not 0.0 <= self.downstream_accuracy <= 1.0:
            raise ValueError("downstream accuracy must be in [0, 1]")


@dataclass(frozen=True)
class CompressionReport:
    """Selected compression and the proxy-versus-end-to-end disagreement."""

    selected: CompressionCandidate | None
    baseline_perplexity: float
    ppl_increase: float
    accuracy: float
    passed: bool
    reversals: int
    local_ranking: tuple[str, ...]
    end_to_end_ranking: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "selected": self.selected.name if self.selected else None,
            "ppl_increase": round(self.ppl_increase, 4),
            "accuracy": round(self.accuracy, 4),
            "passed": self.passed,
            "reversals": self.reversals,
        }


class CompressionBench:
    """Compare local PPL ranking with end-to-end downstream ranking."""

    def local_ranking(self, candidates: Iterable[CompressionCandidate]) -> tuple[str, ...]:
        ordered = tuple(candidates)
        return tuple(c.name for c in sorted(ordered, key=lambda c: (c.perplexity, c.name)))

    def end_to_end_ranking(
        self, candidates: Iterable[CompressionCandidate]
    ) -> tuple[str, ...]:
        ordered = tuple(candidates)
        return tuple(
            c.name
            for c in sorted(
                ordered, key=lambda c: (-c.downstream_accuracy, c.perplexity, c.name)
            )
        )

    def reversals(self, candidates: Iterable[CompressionCandidate]) -> int:
        ordered = tuple(candidates)
        local = self.local_ranking(ordered)
        end = self.end_to_end_ranking(ordered)
        position = {name: index for index, name in enumerate(end)}
        count = 0
        for left in range(len(local)):
            for right in range(left + 1, len(local)):
                if position[local[left]] > position[local[right]]:
                    count += 1
        return count

    def evaluate(
        self,
        candidates: Iterable[CompressionCandidate],
        *,
        baseline_perplexity: float,
        max_ppl_increase: float = 0.034,
        min_accuracy: float = 0.7733,
    ) -> CompressionReport:
        if baseline_perplexity <= 0:
            raise ValueError("baseline perplexity must be positive")
        if max_ppl_increase < 0:
            raise ValueError("max_ppl_increase must not be negative")
        if not 0.0 <= min_accuracy <= 1.0:
            raise ValueError("min_accuracy must be in [0, 1]")
        ordered = tuple(candidates)
        local = self.local_ranking(ordered)
        end = self.end_to_end_ranking(ordered)
        reversals = self.reversals(ordered)
        if not ordered:
            return CompressionReport(
                None, baseline_perplexity, 0.0, 0.0, False, 0, local, end
            )

        selected = min(ordered, key=lambda c: (c.perplexity, c.name))
        ppl_increase = selected.perplexity / baseline_perplexity - 1.0
        passed = (
            selected.independent_verified
            and ppl_increase <= max_ppl_increase + 1e-9
            and selected.downstream_accuracy >= min_accuracy
        )
        return CompressionReport(
            selected=selected,
            baseline_perplexity=baseline_perplexity,
            ppl_increase=ppl_increase,
            accuracy=selected.downstream_accuracy,
            passed=passed,
            reversals=reversals,
            local_ranking=local,
            end_to_end_ranking=end,
        )
