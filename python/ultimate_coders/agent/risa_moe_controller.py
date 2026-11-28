"""Risa (#38) — sparse-MoE route-guided test-time scaling.

Risa's controller never needs an external judge. It reads the routing
signature already produced by a sparse MoE model and turns it into two
behaviours:

* **Disagree to Explore** — during localization, prefer the candidate whose
  route is farthest from the recent history, breaking repeated-search loops.
* **Agree to Commit** — during editing, prefer the candidate that converges
  with same-role peers, stabilising the patch being written.

Across concurrent attempts the controller arbitrates by the routing
consistency of low-confidence decision tokens. Everything is deterministic
bookkeeping over caller-supplied vectors; this module performs no model call.

Ledger item: #38 (arXiv:2608.22191).
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ArbitrationResult",
    "AttemptRoute",
    "RISA_MOE_ENABLED_ENV",
    "RisaMoEController",
    "RouteCandidate",
    "RoutePhase",
    "cosine_similarity",
    "risa_moe_enabled",
]

RISA_MOE_ENABLED_ENV = "UC_RISA_MOE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def risa_moe_enabled() -> bool:
    """Whether the opt-in Risa controller is enabled."""
    return os.environ.get(RISA_MOE_ENABLED_ENV, "").strip().lower() in _TRUTHY


class RoutePhase(str, Enum):
    """Execution phase that selects the routing objective."""

    EXPLORATION = "exploration"
    LOCALIZATION = "localization"
    EDITING = "editing"
    VERIFICATION = "verification"
    COMMIT = "commit"

    @property
    def explores(self) -> bool:
        return self in (RoutePhase.EXPLORATION, RoutePhase.LOCALIZATION)


def cosine_similarity(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Cosine similarity in ``[-1, 1]``; zero vectors return ``0.0``."""
    if len(left) != len(right):
        raise ValueError("routes must have the same dimension")
    # ``zip(strict=...)`` is 3.10+; the harness still supports 3.9, and the
    # caller has already rejected mismatched widths.
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _validated_route(route: Sequence[float], name: str) -> tuple[float, ...]:
    values = tuple(float(value) for value in route)
    if not values:
        raise ValueError(f"{name} must not be empty")
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{name} must contain only finite values")
    return values


@dataclass(frozen=True)
class RouteCandidate:
    """One candidate action with its MoE route signature."""

    id: str
    role: str
    route: tuple[float, ...]
    decision_route: tuple[float, ...] = ()
    decision_confidence: float = 0.5

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("candidate id must not be empty")
        if not self.role.strip():
            raise ValueError("candidate role must not be empty")
        route = _validated_route(self.route, "route")
        decision_route = (
            route
            if not self.decision_route
            else _validated_route(self.decision_route, "decision_route")
        )
        if len(decision_route) != len(route):
            raise ValueError("decision_route must match the route dimension")
        confidence = float(self.decision_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("decision_confidence must be in [0, 1]")
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "decision_route", decision_route)
        object.__setattr__(self, "decision_confidence", confidence)


@dataclass(frozen=True)
class AttemptRoute:
    """Compact route summary used for cross-attempt arbitration."""

    attempt_id: str
    decision_route: tuple[float, ...]
    decision_confidence: float = 0.5

    def __post_init__(self) -> None:
        if not self.attempt_id.strip():
            raise ValueError("attempt_id must not be empty")
        route = _validated_route(self.decision_route, "decision_route")
        confidence = float(self.decision_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("decision_confidence must be in [0, 1]")
        object.__setattr__(self, "decision_route", route)
        object.__setattr__(self, "decision_confidence", confidence)


@dataclass(frozen=True)
class ArbitrationResult:
    """Winner and per-attempt scores for one arbitration round."""

    winner: AttemptRoute | None
    scores: dict[str, float]
    consistency: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "winner": None if self.winner is None else self.winner.attempt_id,
            "scores": dict(self.scores),
            "consistency": dict(self.consistency),
        }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _centroid(routes: Sequence[tuple[float, ...]]) -> tuple[float, ...] | None:
    if not routes:
        return None
    dimension = len(routes[0])
    return tuple(_mean([route[index] for route in routes]) for index in range(dimension))


class RisaMoEController:
    """Pure dual-layer routing controller for sparse-MoE agents."""

    def __init__(
        self,
        *,
        candidates_per_attempt: int = 16,
        concurrent_attempts: int = 4,
        history_window: int = 8,
    ) -> None:
        if candidates_per_attempt <= 0:
            raise ValueError("candidates_per_attempt must be positive")
        if concurrent_attempts <= 0:
            raise ValueError("concurrent_attempts must be positive")
        if history_window <= 0:
            raise ValueError("history_window must be positive")
        self.candidates_per_attempt = candidates_per_attempt
        self.concurrent_attempts = concurrent_attempts
        self.history_window = history_window
        self._history: list[tuple[float, ...]] = []

    @property
    def history(self) -> tuple[tuple[float, ...], ...]:
        return tuple(self._history)

    def record(self, candidate: RouteCandidate) -> None:
        """Append a selected route to the bounded exploration history."""
        self._history.append(candidate.route)
        if len(self._history) > self.history_window:
            del self._history[: len(self._history) - self.history_window]

    def reset(self) -> None:
        """Clear the route history."""
        self._history.clear()

    def exploration_score(self, candidate: RouteCandidate) -> float:
        """Higher means more divergent from recent exploration routes."""
        centroid = _centroid(self._history)
        if centroid is None:
            return 0.0
        return 1.0 - cosine_similarity(candidate.route, centroid)

    def commitment_score(
        self,
        candidate: RouteCandidate,
        candidates: Sequence[RouteCandidate],
    ) -> float:
        """Higher means stronger convergence with same-role peers."""
        peers = [
            peer.route
            for peer in candidates
            if peer.id != candidate.id and peer.role == candidate.role
        ]
        if not peers:
            return 0.0
        return _mean([cosine_similarity(candidate.route, peer) for peer in peers])

    def select(
        self,
        candidates: Sequence[RouteCandidate],
        phase: RoutePhase | str,
        *,
        record: bool = True,
    ) -> RouteCandidate | None:
        """Select one candidate for a phase, or ``None`` for an empty list."""
        if not candidates:
            return None
        resolved_phase = RoutePhase(phase)
        if resolved_phase.explores:

            def key(candidate: RouteCandidate) -> tuple[float, str]:
                return (-self.exploration_score(candidate), candidate.id)

        else:

            def key(candidate: RouteCandidate) -> tuple[float, str]:
                return (-self.commitment_score(candidate, candidates), candidate.id)

        chosen = min(candidates, key=key)
        if record:
            self.record(chosen)
        return chosen

    def arbitrate(
        self,
        attempts: Sequence[AttemptRoute],
    ) -> ArbitrationResult:
        """Choose the attempt with the most consistent decision route.

        Consistency is the mean pairwise cosine similarity, rescaled to
        ``[0, 1]``. A low decision confidence receives a small tie-break bonus
        because it marks a genuine routing fork rather than a saturated token.
        """
        if not attempts:
            return ArbitrationResult(None, {}, {})
        scores: dict[str, float] = {}
        consistency: dict[str, float] = {}
        for attempt in attempts:
            peers = [
                cosine_similarity(attempt.decision_route, other.decision_route)
                for other in attempts
                if other.attempt_id != attempt.attempt_id
            ]
            mean_cosine = _mean(peers)
            normalised = (mean_cosine + 1.0) / 2.0
            low_confidence_bonus = 1.0 - attempt.decision_confidence
            consistency[attempt.attempt_id] = mean_cosine
            scores[attempt.attempt_id] = (
                0.75 * normalised + 0.25 * low_confidence_bonus
            )
        winner = min(
            attempts,
            key=lambda attempt: (
                -scores[attempt.attempt_id],
                attempt.decision_confidence,
                attempt.attempt_id,
            ),
        )
        return ArbitrationResult(winner, scores, consistency)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates_per_attempt": self.candidates_per_attempt,
            "concurrent_attempts": self.concurrent_attempts,
            "history_window": self.history_window,
            "history_size": len(self._history),
        }
