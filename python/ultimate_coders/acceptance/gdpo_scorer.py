"""GDPO component-normalised scoring (v10 #21).

Group-relative preference optimisation compares several rollouts of the same
task.  Raw component rates (pass rate, fail-to-pass rate, pass-to-pass rate)
are not directly comparable across tasks: one task may have a very hard
fail-to-pass component, another an easy one.  GDPO therefore normalises each
component **within its own rollout group** before combining them, so a small
group improvement in a hard component is not drowned out by an easy one.

The implementation is pure, deterministic and stdlib-only:

* :class:`ComponentScores` is one rollout's three component rates.
* :class:`GDPOWeight` holds the combination weights (0.5 / 0.3 / 0.2 by
  default, summing to 1).
* :class:`GDPOGroup` performs per-component z-score normalisation over a
  group and returns weighted per-rollout rewards from
  :meth:`GDPOGroup.weighted_batch_norm`.

Degenerate groups are handled without special cases in the caller: an empty
group returns empty tuples, and a component with zero standard deviation
normalises to all zeros so it contributes nothing.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "GDPO_SCORER_ENV",
    "ComponentScores",
    "GDPOGroup",
    "GDPOWeight",
    "gdpo_scorer_enabled",
]

#: Environment flag that enables GDPO scoring in an acceptance pipeline.
GDPO_SCORER_ENV = "UC_GDPO_SCORER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_WEIGHT_KEYS = ("pass_rate", "f2p_rate", "p2p_rate")


def gdpo_scorer_enabled() -> bool:
    """Return whether the GDPO scorer is enabled via ``UC_GDPO_SCORER``."""

    return os.environ.get(GDPO_SCORER_ENV, "").strip().lower() in _TRUTHY


def _coerce_score(value: object, name: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a real number") from exc
    if not math.isfinite(score):
        raise ValueError(f"{name} must be finite")
    return score


@dataclass(frozen=True)
class ComponentScores:
    """One rollout's component rates for a GDPO group."""

    pass_rate: float
    f2p_rate: float
    p2p_rate: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "pass_rate", _coerce_score(self.pass_rate, "pass_rate"))
        object.__setattr__(self, "f2p_rate", _coerce_score(self.f2p_rate, "f2p_rate"))
        object.__setattr__(self, "p2p_rate", _coerce_score(self.p2p_rate, "p2p_rate"))

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.pass_rate, self.f2p_rate, self.p2p_rate)

    def as_dict(self) -> dict[str, float]:
        return dict(zip(_WEIGHT_KEYS, self.as_tuple()))

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> ComponentScores:
        return cls(
            pass_rate=values.get("pass_rate", 0.0),
            f2p_rate=values.get("f2p_rate", 0.0),
            p2p_rate=values.get("p2p_rate", 0.0),
        )


@dataclass(frozen=True)
class GDPOWeight:
    """Component combination weights.

    The defaults sum to exactly 1.0.  A caller may supply other weights;
    :meth:`normalised` rescales them by their sum, and
    :meth:`GDPOGroup.weighted_batch_norm` always renormalises defensively, so
    the final reward stays on the same scale regardless of how the weights
    were authored.
    """

    pass_rate: float = 0.5
    f2p_rate: float = 0.3
    p2p_rate: float = 0.2

    def __post_init__(self) -> None:
        object.__setattr__(self, "pass_rate", _coerce_score(self.pass_rate, "pass_rate"))
        object.__setattr__(self, "f2p_rate", _coerce_score(self.f2p_rate, "f2p_rate"))
        object.__setattr__(self, "p2p_rate", _coerce_score(self.p2p_rate, "p2p_rate"))
        if self.total <= 0:
            raise ValueError("GDPO weights must sum to a positive value")

    @property
    def total(self) -> float:
        return self.pass_rate + self.f2p_rate + self.p2p_rate

    def normalised(self) -> GDPOWeight:
        """Return an equivalent weight object whose components sum to 1."""

        total = self.total
        return GDPOWeight(
            pass_rate=self.pass_rate / total,
            f2p_rate=self.f2p_rate / total,
            p2p_rate=self.p2p_rate / total,
        )

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.pass_rate, self.f2p_rate, self.p2p_rate)

    def as_dict(self) -> dict[str, float]:
        return dict(zip(_WEIGHT_KEYS, self.as_tuple()))


class GDPOGroup:
    """A group of rollouts scored with per-component group normalisation."""

    def __init__(
        self,
        scores: Iterable[ComponentScores | Mapping[str, object]] = (),
        *,
        weight: GDPOWeight | None = None,
    ) -> None:
        self._scores = tuple(_coerce_scores(scores))
        self.weight = weight if weight is not None else GDPOWeight()

    @property
    def scores(self) -> tuple[ComponentScores, ...]:
        return self._scores

    def __len__(self) -> int:
        return len(self._scores)

    def __iter__(self):
        return iter(self._scores)

    def normalise(self) -> tuple[ComponentScores, ...]:
        """Z-score every component across the group, independently.

        Population standard deviation is used (divide by ``n``), which keeps
        the result symmetric for small groups.  A zero-variance component
        becomes all zeros instead of dividing by zero.
        """

        count = len(self._scores)
        if count == 0:
            return ()
        columns = list(zip(*(score.as_tuple() for score in self._scores)))
        normalised: list[tuple[float, float, float]] = []
        for column in columns:
            mean = sum(column) / count
            variance = sum((value - mean) ** 2 for value in column) / count
            std = math.sqrt(variance)
            if std == 0.0:
                normalised.append(tuple(0.0 for _ in column))
            else:
                normalised.append(tuple((value - mean) / std for value in column))
        return tuple(
            ComponentScores(pass_rate=pass_rate, f2p_rate=f2p_rate, p2p_rate=p2p_rate)
            for pass_rate, f2p_rate, p2p_rate in zip(*normalised)
        )

    def weighted_batch_norm(self, *, weight: GDPOWeight | None = None) -> tuple[float, ...]:
        """Return the weighted sum of normalised components per rollout.

        Weights are renormalised by their total before use, so passing
        ``GDPOWeight(1, 1, 1)`` behaves identically to the default weights.
        The output order matches the input rollout order and is deterministic.
        """

        chosen = weight if weight is not None else self.weight
        used = chosen.normalised()
        rewards: list[float] = []
        for score in self.normalise():
            rewards.append(
                used.pass_rate * score.pass_rate
                + used.f2p_rate * score.f2p_rate
                + used.p2p_rate * score.p2p_rate
            )
        return tuple(rewards)

    def summary(self) -> dict[str, object]:
        """Compact description for events and tests (no rollout payloads)."""

        rewards = self.weighted_batch_norm()
        return {
            "count": len(self._scores),
            "weight": self.weight.as_dict(),
            "rewards": list(rewards),
        }


def _coerce_scores(
    scores: Iterable[ComponentScores | Mapping[str, object]],
) -> list[ComponentScores]:
    coerced: list[ComponentScores] = []
    for value in scores:
        if isinstance(value, ComponentScores):
            coerced.append(value)
        elif isinstance(value, Mapping):
            coerced.append(ComponentScores.from_mapping(value))
        else:
            raise TypeError("GDPO group entries must be ComponentScores or mappings")
    return coerced
