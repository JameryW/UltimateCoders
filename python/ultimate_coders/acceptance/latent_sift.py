"""LatentSift: zero-token candidate filtering over generator hidden states (#27).

Ledger item #27 adapts LatentSift (arXiv:2609.36371).  Best-of-K verification is
dominated by re-reading K long trajectories with an LLM verifier.  LatentSift
reuses hidden states the generator already produced, pools them into three
channels (reasoning / observation / function), and ranks candidates by their
nearest-neighbour margin against positive and negative experience banks.  The
top half of the pool is kept for expensive verification, cutting roughly 60%
of verifier tokens without extra model calls.

Feature flag: ``UC_LATENT_SIFT`` (default off).
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "UC_LATENT_SIFT",
    "CHANNELS",
    "ChannelTrace",
    "ExperienceBank",
    "LatentCandidate",
    "CandidateScore",
    "SiftReport",
    "LatentSiftFilter",
    "latent_sift_enabled",
    "sift_candidates",
]

UC_LATENT_SIFT = "UC_LATENT_SIFT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
CHANNELS = ("reasoning", "observation", "function")
_EPSILON = 1e-12


def latent_sift_enabled() -> bool:
    """Return whether the zero-token latent candidate filter is enabled."""

    return os.environ.get(UC_LATENT_SIFT, "").strip().lower() in _TRUTHY


def _validate_vector(vector: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("latent vectors must be finite")
    return values


@dataclass(frozen=True)
class ChannelTrace:
    """Per-channel hidden-state traces for one candidate.

    Each channel holds a sequence of step vectors, one per tool/observation
    boundary.  Empty channels are allowed but contribute no ranking signal.
    """

    reasoning: tuple[tuple[float, ...], ...] = ()
    observation: tuple[tuple[float, ...], ...] = ()
    function: tuple[tuple[float, ...], ...] = ()

    def __post_init__(self) -> None:
        for name in CHANNELS:
            steps = tuple(_validate_vector(step) for step in getattr(self, name))
            if steps:
                width = len(steps[0])
                if any(len(step) != width for step in steps):
                    raise ValueError(f"inconsistent vector width in {name}")
            object.__setattr__(self, name, steps)

    def channel(self, name: str) -> tuple[tuple[float, ...], ...]:
        if name not in CHANNELS:
            raise KeyError(name)
        return getattr(self, name)

    def time_weighted_pool(self, name: str) -> tuple[float, ...]:
        """Linearly weight later steps more heavily, then mean-pool."""

        steps = self.channel(name)
        if not steps:
            return ()
        weights = [float(index + 1) for index in range(len(steps))]
        total = sum(weights)
        width = len(steps[0])
        return tuple(
            sum(step[dimension] * weight for step, weight in zip(steps, weights))
            / total
            for dimension in range(width)
        )

    def to_dict(self) -> dict[str, int]:
        return {name: len(self.channel(name)) for name in CHANNELS}


@dataclass(frozen=True)
class ExperienceBank:
    """Positive and negative reference vectors per channel."""

    positive: Mapping[str, Sequence[Sequence[float]]] = field(default_factory=dict)
    negative: Mapping[str, Sequence[Sequence[float]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, bank in (("positive", self.positive), ("negative", self.negative)):
            normalised: dict[str, tuple[tuple[float, ...], ...]] = {}
            for channel in CHANNELS:
                vectors = tuple(_validate_vector(vector) for vector in bank.get(channel, ()))
                if vectors:
                    width = len(vectors[0])
                    if any(len(vector) != width for vector in vectors):
                        raise ValueError(f"inconsistent bank vector width in {label}/{channel}")
                normalised[channel] = vectors
            object.__setattr__(self, label, normalised)

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {
            "positive": {name: len(self.positive[name]) for name in CHANNELS},
            "negative": {name: len(self.negative[name]) for name in CHANNELS},
        }


@dataclass(frozen=True)
class LatentCandidate:
    candidate_id: str
    trace: ChannelTrace
    label: str = ""

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise ValueError("candidate_id must be non-empty")
        object.__setattr__(self, "candidate_id", str(self.candidate_id).strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "label": self.label,
            "trace": self.trace.to_dict(),
        }


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    channel_margins: Mapping[str, float]
    channel_ranks: Mapping[str, int]
    distance_rank: int
    linear_rank: int
    blended_rank: float
    selected: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "channel_margins": dict(self.channel_margins),
            "channel_ranks": dict(self.channel_ranks),
            "distance_rank": self.distance_rank,
            "linear_rank": self.linear_rank,
            "blended_rank": round(self.blended_rank, 6),
            "selected": self.selected,
        }


@dataclass(frozen=True)
class SiftReport:
    scores: tuple[CandidateScore, ...]
    selected: tuple[str, ...]
    dropped: tuple[str, ...]
    top_k: int
    blend_weight: float

    @property
    def oracle_retention(self) -> float:
        """Share of the pool retained for oracle/verifier evaluation."""

        total = len(self.selected) + len(self.dropped)
        return round(len(self.selected) / total, 6) if total else 1.0

    def to_dict(self) -> dict[str, object]:
        return {
            "scores": [score.to_dict() for score in self.scores],
            "selected": list(self.selected),
            "dropped": list(self.dropped),
            "top_k": self.top_k,
            "blend_weight": self.blend_weight,
            "oracle_retention": self.oracle_retention,
        }


def _euclidean(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vector dimensions do not match")
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def _nearest(vector: Sequence[float], bank: Iterable[Sequence[float]]) -> float:
    # Banks may carry vectors from a different encoder width.  Skip those
    # incompatible references instead of failing the whole candidate.
    vectors = [reference for reference in bank if len(reference) == len(vector)]
    if not vectors:
        return math.inf
    return min(_euclidean(vector, reference) for reference in vectors)


class LatentSiftFilter:
    """Rank candidates by channel margins blended with a linear scorer."""

    def __init__(
        self,
        positive_bank: ExperienceBank,
        negative_bank: ExperienceBank,
        *,
        top_k: int = 8,
        blend_weight: float = 0.5,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not 0.0 <= blend_weight <= 1.0:
            raise ValueError("blend_weight must be in [0, 1]")
        self.positive_bank = positive_bank
        self.negative_bank = negative_bank
        self.top_k = top_k
        self.blend_weight = blend_weight

    def score(
        self,
        candidates: Sequence[LatentCandidate],
        linear_scores: Mapping[str, float] | None = None,
    ) -> SiftReport:
        """Score and rank candidates without any additional model call."""

        if not candidates:
            return SiftReport((), (), (), self.top_k, self.blend_weight)
        linear_scores = linear_scores or {}

        margins: dict[str, dict[str, float]] = {}
        for candidate in candidates:
            candidate_margins: dict[str, float] = {}
            for channel in CHANNELS:
                vector = candidate.trace.time_weighted_pool(channel)
                if not vector:
                    continue
                positive_distance = _nearest(
                    vector, self.positive_bank.positive.get(channel, ())
                )
                negative_distance = _nearest(
                    vector, self.negative_bank.negative.get(channel, ())
                )
                if math.isinf(positive_distance) and math.isinf(negative_distance):
                    continue
                if math.isinf(positive_distance):
                    margin = -negative_distance
                elif math.isinf(negative_distance):
                    margin = positive_distance
                else:
                    margin = negative_distance - positive_distance
                candidate_margins[channel] = round(margin, 9)
            margins[candidate.candidate_id] = candidate_margins

        channel_ranks = self._rank_channels(candidates, margins)
        distance_ranks: dict[str, int] = {}
        for candidate in candidates:
            ranks = channel_ranks.get(candidate.candidate_id, {})
            distance_ranks[candidate.candidate_id] = (
                min(ranks.values()) if ranks else len(candidates) + 1
            )

        ordered_linear = sorted(
            candidates,
            key=lambda item: (
                -float(linear_scores.get(item.candidate_id, 0.0)),
                item.candidate_id,
            ),
        )
        linear_ranks = {
            candidate.candidate_id: index + 1
            for index, candidate in enumerate(ordered_linear)
        }

        blended: dict[str, float] = {}
        for candidate in candidates:
            blended[candidate.candidate_id] = (
                self.blend_weight * distance_ranks[candidate.candidate_id]
                + (1.0 - self.blend_weight) * linear_ranks[candidate.candidate_id]
            )

        def ranking_key(item: LatentCandidate) -> tuple[float, float, int, str]:
            margins_for_item = margins.get(item.candidate_id, {})
            return (
                blended[item.candidate_id],
                -sum(margins_for_item.values()),
                linear_ranks[item.candidate_id],
                item.candidate_id,
            )

        ordered = sorted(candidates, key=ranking_key)
        selected = {candidate.candidate_id for candidate in ordered[: self.top_k]}

        scores = tuple(
            CandidateScore(
                candidate_id=candidate.candidate_id,
                channel_margins=margins[candidate.candidate_id],
                channel_ranks=channel_ranks.get(candidate.candidate_id, {}),
                distance_rank=distance_ranks[candidate.candidate_id],
                linear_rank=linear_ranks[candidate.candidate_id],
                blended_rank=blended[candidate.candidate_id],
                selected=candidate.candidate_id in selected,
            )
            for candidate in ordered
        )
        return SiftReport(
            scores=scores,
            selected=tuple(candidate.candidate_id for candidate in ordered[: self.top_k]),
            dropped=tuple(
                candidate.candidate_id for candidate in ordered[self.top_k :]
            ),
            top_k=self.top_k,
            blend_weight=self.blend_weight,
        )

    @staticmethod
    def _rank_channels(
        candidates: Sequence[LatentCandidate],
        margins: Mapping[str, Mapping[str, float]],
    ) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {
            candidate.candidate_id: {} for candidate in candidates
        }
        for channel in CHANNELS:
            scored = [
                (margins[candidate.candidate_id][channel], candidate.candidate_id)
                for candidate in candidates
                if channel in margins[candidate.candidate_id]
            ]
            scored.sort(key=lambda pair: (-pair[0], pair[1]))
            for index, (_, candidate_id) in enumerate(scored):
                result[candidate_id][channel] = index + 1
        return result


def sift_candidates(
    candidates: Sequence[LatentCandidate],
    positive_bank: ExperienceBank,
    negative_bank: ExperienceBank,
    *,
    linear_scores: Mapping[str, float] | None = None,
    top_k: int = 8,
) -> SiftReport:
    """Convenience wrapper for one-shot filtering."""

    return LatentSiftFilter(
        positive_bank, negative_bank, top_k=top_k
    ).score(candidates, linear_scores)
