"""HSE: cross-harness hierarchical skill evolution and performance audit (#36).

Ledger item #36 adapts Hierarchical Skill Evolution (arXiv:2610.35616) and
MicroPerf-Bench (arXiv:2610.29695).  Skill extraction tends to bake in
task-specific details and then fail to transfer across harnesses or model
families.  The evolver separates an external writer from an evaluator: the
writer proposes procedure cards, the evaluator keeps only cards with measured
positive transfer on at least two harnesses.

The same module carries nine system-level performance anti-pattern probes so a
BenchmarkRunner can report *why* a microservice optimisation failed to recover
latency, not just whether the functional tests passed.

Feature flag: ``UC_HSE_EVOLVER`` (default off).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "UC_HSE_EVOLVER",
    "HarnessKind",
    "PerformanceAntiPattern",
    "SkillEvidence",
    "SkillCard",
    "SkillEvaluation",
    "SkillEvolutionReport",
    "PerformanceAudit",
    "HseEvolver",
    "hse_evolver_enabled",
    "audit_performance",
]

UC_HSE_EVOLVER = "UC_HSE_EVOLVER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEFAULT_TARGET_RECOVERY = 0.42


def hse_evolver_enabled() -> bool:
    """Return whether hierarchical skill evolution is enabled."""

    return os.environ.get(UC_HSE_EVOLVER, "").strip().lower() in _TRUTHY


class HarnessKind(str, Enum):
    PI = "pi"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class PerformanceAntiPattern(str, Enum):
    LOCK_CONTENTION = "lock_contention"
    N_PLUS_ONE_QUERY = "n_plus_one_query"
    BLOCKING_IO = "blocking_io"
    SERIALIZATION_HOTSPOT = "serialization_hotspot"
    UNBOUNDED_CACHE = "unbounded_cache"
    MISSING_INDEX = "missing_index"
    CHATTY_RPC = "chatty_rpc"
    COLD_START = "cold_start"
    ALLOCATION_CHURN = "allocation_churn"


@dataclass(frozen=True)
class SkillEvidence:
    """One measured result of a skill card on a harness and model."""

    harness: HarnessKind
    model: str
    trials: int
    successes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", str(self.model).strip())
        object.__setattr__(self, "trials", max(0, int(self.trials)))
        object.__setattr__(self, "successes", min(self.trials, max(0, int(self.successes))))

    @property
    def success_rate(self) -> float:
        return self.successes / self.trials if self.trials else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "harness": self.harness.value,
            "model": self.model,
            "trials": self.trials,
            "successes": self.successes,
            "success_rate": round(self.success_rate, 6),
        }


@dataclass(frozen=True)
class SkillCard:
    """A procedure card written by the external writer."""

    name: str
    description: str
    applicability: tuple[str, ...] = ()
    procedure: tuple[str, ...] = ()
    evidence: tuple[SkillEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "description", str(self.description).strip())
        object.__setattr__(
            self, "applicability", tuple(str(value).strip() for value in self.applicability)
        )
        object.__setattr__(self, "procedure", tuple(str(value).strip() for value in self.procedure))

    @property
    def average_success_rate(self) -> float:
        trials = sum(item.trials for item in self.evidence)
        successes = sum(item.successes for item in self.evidence)
        return successes / trials if trials else 0.0

    @property
    def positive_harnesses(self) -> tuple[HarnessKind, ...]:
        seen: list[HarnessKind] = []
        for item in self.evidence:
            if item.success_rate >= 0.5 and item.harness not in seen:
                seen.append(item.harness)
        return tuple(seen)

    def is_generic(self, *, min_harnesses: int = 2, min_success_rate: float = 0.5) -> bool:
        return (
            len(self.positive_harnesses) >= min_harnesses
            and self.average_success_rate >= min_success_rate
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "applicability": list(self.applicability),
            "procedure": list(self.procedure),
            "evidence": [item.to_dict() for item in self.evidence],
            "average_success_rate": round(self.average_success_rate, 6),
            "positive_harnesses": [item.value for item in self.positive_harnesses],
        }


@dataclass(frozen=True)
class SkillEvaluation:
    card: SkillCard
    generic: bool
    positive_harnesses: tuple[HarnessKind, ...]
    average_success_rate: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "card": self.card.to_dict(),
            "generic": self.generic,
            "positive_harnesses": [item.value for item in self.positive_harnesses],
            "average_success_rate": round(self.average_success_rate, 6),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SkillEvolutionReport:
    accepted: tuple[SkillCard, ...]
    rejected: tuple[SkillCard, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": [card.to_dict() for card in self.accepted],
            "rejected": [card.to_dict() for card in self.rejected],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class PerformanceAudit:
    baseline_ms: float
    optimized_ms: float
    recovery_rate: float
    target_recovery: float
    anti_patterns: tuple[PerformanceAntiPattern, ...]
    passed: bool
    diagnosis: str

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline_ms": round(self.baseline_ms, 6),
            "optimized_ms": round(self.optimized_ms, 6),
            "recovery_rate": round(self.recovery_rate, 6),
            "target_recovery": round(self.target_recovery, 6),
            "anti_patterns": [item.value for item in self.anti_patterns],
            "passed": self.passed,
            "diagnosis": self.diagnosis,
        }


class HseEvolver:
    """External writer/evaluator loop that keeps transferable procedures."""

    def __init__(
        self,
        *,
        min_harnesses: int = 2,
        min_success_rate: float = 0.5,
        max_skills: int = 10,
    ) -> None:
        if min_harnesses < 1:
            raise ValueError("min_harnesses must be >= 1")
        if not 0.0 <= min_success_rate <= 1.0:
            raise ValueError("min_success_rate must be in [0, 1]")
        self.min_harnesses = min_harnesses
        self.min_success_rate = min_success_rate
        self.max_skills = max(1, int(max_skills))
        self._written: list[SkillCard] = []

    @property
    def written(self) -> tuple[SkillCard, ...]:
        return tuple(self._written)

    def write_candidate(
        self,
        name: str,
        description: str,
        *,
        applicability: Iterable[str] = (),
        procedure: Iterable[str] = (),
        evidence: Iterable[SkillEvidence] = (),
    ) -> SkillCard:
        """Writer role: propose one procedure card without evaluating it."""

        card = SkillCard(
            name=name,
            description=description,
            applicability=tuple(applicability),
            procedure=tuple(procedure),
            evidence=tuple(evidence),
        )
        self._written.append(card)
        return card

    def evaluate(self, card: SkillCard) -> SkillEvaluation:
        """Evaluator role: keep only cross-harness transfers above threshold."""

        positive = card.positive_harnesses
        generic = card.is_generic(
            min_harnesses=self.min_harnesses,
            min_success_rate=self.min_success_rate,
        )
        if generic:
            reason = (
                f"positive transfer on {len(positive)} harnesses at "
                f"{card.average_success_rate:.3f} weighted success"
            )
        elif len(positive) < self.min_harnesses:
            reason = (
                f"only {len(positive)} harness(es) above 50% success; need {self.min_harnesses}"
            )
        else:
            reason = (
                f"weighted success {card.average_success_rate:.3f} is below "
                f"{self.min_success_rate:.3f}"
            )
        return SkillEvaluation(
            card=card,
            generic=generic,
            positive_harnesses=positive,
            average_success_rate=card.average_success_rate,
            reason=reason,
        )

    def evolve(self, cards: Sequence[SkillCard] | None = None) -> SkillEvolutionReport:
        """Evaluate writer candidates and keep the strongest transferable cards."""

        pool = list(cards if cards is not None else self._written)
        evaluations = [self.evaluate(card) for card in pool]
        accepted = [item for item in evaluations if item.generic]
        rejected = [item for item in evaluations if not item.generic]
        accepted.sort(
            key=lambda item: (-item.average_success_rate, item.card.name),
        )
        accepted = accepted[: self.max_skills]
        reasons = tuple(
            [f"accepted: {item.card.name} ({item.reason})" for item in accepted]
            + [f"rejected: {item.card.name} ({item.reason})" for item in rejected]
        )
        return SkillEvolutionReport(
            accepted=tuple(item.card for item in accepted),
            rejected=tuple(item.card for item in rejected),
            reasons=reasons,
        )


def audit_performance(
    baseline_ms: float,
    optimized_ms: float,
    anti_patterns: Iterable[PerformanceAntiPattern] = (),
    *,
    target_recovery: float = _DEFAULT_TARGET_RECOVERY,
) -> PerformanceAudit:
    """Report end-to-end latency recovery and the detected system-level causes."""

    baseline = max(0.0, float(baseline_ms))
    optimized = max(0.0, float(optimized_ms))
    if baseline <= 0:
        raise ValueError("baseline_ms must be positive")
    recovery = max(0.0, (baseline - optimized) / baseline)
    detected = tuple(dict.fromkeys(anti_patterns))
    passed = recovery >= target_recovery
    if passed:
        diagnosis = "latency recovery meets the MicroPerf target"
    elif detected:
        diagnosis = "functional fix did not reach the system-level bottleneck: " + ", ".join(
            item.value for item in detected
        )
    else:
        diagnosis = "latency recovery is below target and no anti-pattern was identified"
    return PerformanceAudit(
        baseline_ms=baseline,
        optimized_ms=optimized,
        recovery_rate=round(recovery, 6),
        target_recovery=target_recovery,
        anti_patterns=detected,
        passed=passed,
        diagnosis=diagnosis,
    )
