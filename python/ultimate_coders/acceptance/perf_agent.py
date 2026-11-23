"""PerfAgent profiler-guided optimization loop (#76).

The agent ranks deterministic CPU/memory profile samples and evaluates a
stream of candidate patches.  A candidate is accepted only when it keeps the
functional suite green, improves the measured speedup over the current best,
and reduces the share of samples spent in the targeted hot stack.  The module
is pure bookkeeping: callers run the profiler, apply the patch, and execute the
tests.

Flag: ``UC_PERF_AGENT`` (default off).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "PERF_AGENT_ENV",
    "OptimizationCandidate",
    "PerfAgent",
    "PerfAttempt",
    "PerfReport",
    "ProfileSample",
    "perf_agent_enabled",
]

PERF_AGENT_ENV = "UC_PERF_AGENT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def perf_agent_enabled() -> bool:
    """Return whether PerfAgent is enabled."""

    return os.environ.get(PERF_AGENT_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ProfileSample:
    """One profiler hotspot sample."""

    function: str
    samples: int
    stack_share: float
    file: str = ""

    def __post_init__(self) -> None:
        if not self.function.strip():
            raise ValueError("function must not be empty")
        if self.samples < 0:
            raise ValueError("samples must not be negative")
        if not 0.0 <= self.stack_share <= 1.0:
            raise ValueError("stack_share must be between 0 and 1")


@dataclass(frozen=True)
class OptimizationCandidate:
    """One candidate optimization patch and its measured result."""

    id: str
    patch: str
    speedup: float
    tests_passed: bool
    hot_share_before: float
    hot_share_after: float

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.patch.strip():
            raise ValueError("id and patch must not be empty")
        if self.speedup <= 0:
            raise ValueError("speedup must be positive")
        if not 0.0 <= self.hot_share_before <= 1.0:
            raise ValueError("hot_share_before must be between 0 and 1")
        if not 0.0 <= self.hot_share_after <= 1.0:
            raise ValueError("hot_share_after must be between 0 and 1")


@dataclass(frozen=True)
class PerfAttempt:
    """Decision for one candidate."""

    candidate_id: str
    accepted: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PerfReport:
    """Final outcome of an optimization run."""

    accepted: tuple[str, ...]
    best_speedup: float
    attempts: tuple[PerfAttempt, ...]
    hot_functions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": list(self.accepted),
            "best_speedup": self.best_speedup,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "hot_functions": list(self.hot_functions),
        }


class PerfAgent:
    """Rank profiler hotspots and accept monotone optimization patches."""

    def __init__(self, min_speedup: float = 1.05, max_rounds: int = 3) -> None:
        if min_speedup <= 1.0:
            raise ValueError("min_speedup must be greater than 1")
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.min_speedup = min_speedup
        self.max_rounds = max_rounds

    def hotspots(
        self,
        samples: tuple[ProfileSample, ...] | list[ProfileSample],
        limit: int = 5,
    ) -> tuple[ProfileSample, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        ranked = sorted(
            samples,
            key=lambda sample: (-(sample.samples * max(sample.stack_share, 1e-9)), sample.function),
        )
        return tuple(ranked[:limit])

    def evaluate(
        self,
        candidate: OptimizationCandidate,
        *,
        current_speedup: float = 1.0,
    ) -> PerfAttempt:
        if current_speedup <= 0:
            raise ValueError("current_speedup must be positive")
        if not candidate.tests_passed:
            return PerfAttempt(candidate.id, False, "functional tests failed")
        if candidate.speedup < max(self.min_speedup, current_speedup):
            return PerfAttempt(candidate.id, False, "speedup did not improve")
        if candidate.hot_share_after >= candidate.hot_share_before:
            return PerfAttempt(candidate.id, False, "hot stack share did not decrease")
        return PerfAttempt(candidate.id, True, "accepted")

    def optimize(
        self,
        candidates: tuple[OptimizationCandidate, ...] | list[OptimizationCandidate],
        hotspots: tuple[ProfileSample, ...] | list[ProfileSample] = (),
    ) -> PerfReport:
        accepted: list[str] = []
        attempts: list[PerfAttempt] = []
        best = 1.0
        for candidate in candidates[: self.max_rounds]:
            attempt = self.evaluate(candidate, current_speedup=best)
            attempts.append(attempt)
            if attempt.accepted:
                accepted.append(candidate.id)
                best = candidate.speedup
        hot = tuple(sample.function for sample in self.hotspots(hotspots)) if hotspots else ()
        return PerfReport(tuple(accepted), best, tuple(attempts), hot)
