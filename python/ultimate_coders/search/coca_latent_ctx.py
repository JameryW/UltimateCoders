"""Coca-Cola latent tool-observation compression (ledger item #116).

The upstream observation channel is text tokens, which is a poor fit for long
noisy tool outputs: the model pays to read shell logs, JSON dumps and test
output that barely change the next action.  Coca-Cola (arXiv:2611.06500)
instead maps the observation to continuous latent tokens and trains the
policy so the next-action distribution is preserved.

This module is a deterministic, dependency-free stand-in for that interface:
an adapter turns an observation into a bounded tuple of latent tokens and the
caller can compare the next-action distributions before and after the
compression.  It does not embed, train or call a model.

Environment:
    UC_COCA_LATENT_CTX=1 to advertise the feature.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "COCA_LATENT_CTX_ENV",
    "ActionDistribution",
    "CocaLatentCompressor",
    "CocaLatentError",
    "LatentObservation",
    "LatentToken",
    "coca_latent_ctx_enabled",
]

COCA_LATENT_CTX_ENV = "UC_COCA_LATENT_CTX"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_WHITESPACE = re.compile(r"\S+")


def coca_latent_ctx_enabled() -> bool:
    """Return whether the opt-in flag is set."""

    return os.environ.get(COCA_LATENT_CTX_ENV, "").strip().lower() in _TRUTHY


def _fnv1a(value: str) -> int:
    digest = 0x811C9DC5
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest


class CocaLatentError(ValueError):
    """Raised when an observation or action distribution is invalid."""


@dataclass(frozen=True)
class LatentToken:
    """One continuous-space token stand-in."""

    token_id: int
    weight: float

    def __post_init__(self) -> None:
        if self.token_id < 0:
            raise CocaLatentError("token_id must be non-negative")
        if not math.isfinite(self.weight) or self.weight <= 0.0:
            raise CocaLatentError("weight must be finite and positive")


@dataclass(frozen=True)
class LatentObservation:
    """Compressed representation of one tool observation."""

    tool: str
    step: int
    latent_tokens: tuple[LatentToken, ...]
    source_tokens: int
    source_chars: int

    def __post_init__(self) -> None:
        if not self.tool.strip():
            raise CocaLatentError("tool must not be empty")
        if self.step < 0:
            raise CocaLatentError("step must be non-negative")
        if self.source_tokens < 1:
            raise CocaLatentError("source_tokens must be positive")
        if not self.latent_tokens:
            raise CocaLatentError("latent_tokens must not be empty")

    @property
    def token_count(self) -> int:
        """Number of latent tokens."""

        return len(self.latent_tokens)

    @property
    def compression_ratio(self) -> float:
        """Compressed over source tokens."""

        return self.token_count / self.source_tokens

    @property
    def preview(self) -> str:
        """A compact diagnostic preview."""

        ids = ",".join(str(token.token_id) for token in self.latent_tokens[:4])
        return (
            f"[{self.tool}@{self.step}: "
            f"{self.token_count}/{self.source_tokens} tokens; ids={ids}]"
        )

    def to_dict(self) -> dict[str, object]:
        """Serialise the observation."""

        return {
            "tool": self.tool,
            "step": self.step,
            "token_count": self.token_count,
            "source_tokens": self.source_tokens,
            "source_chars": self.source_chars,
            "compression_ratio": self.compression_ratio,
            "tokens": [
                {"token_id": token.token_id, "weight": token.weight}
                for token in self.latent_tokens
            ],
        }


@dataclass(frozen=True)
class ActionDistribution:
    """A normalised next-action distribution."""

    weights: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not self.weights:
            raise CocaLatentError("action distribution must not be empty")
        actions: set[str] = set()
        total = 0.0
        for action, probability in self.weights:
            if not action.strip():
                raise CocaLatentError("action must not be empty")
            if action in actions:
                raise CocaLatentError(f"duplicate action: {action}")
            actions.add(action)
            if not math.isfinite(probability) or probability <= 0.0:
                raise CocaLatentError("action probabilities must be finite and positive")
            total += probability
        if abs(total - 1.0) > 1e-6:
            raise CocaLatentError("action probabilities must sum to 1")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, float]) -> ActionDistribution:
        """Build from a mapping, preserving iteration order."""

        return cls(tuple((str(action), float(value)) for action, value in mapping.items()))

    def probability(self, action: str) -> float:
        """Return the probability of one action, or zero."""

        for name, probability in self.weights:
            if name == action:
                return probability
        return 0.0

    @property
    def actions(self) -> tuple[str, ...]:
        """Actions in stored order."""

        return tuple(action for action, _ in self.weights)


class CocaLatentCompressor:
    """Bounded, deterministic latent-token adapter."""

    def __init__(self, *, ratio: float = 0.2, max_tokens: int = 128) -> None:
        if not math.isfinite(ratio) or not 0.0 < ratio <= 1.0:
            raise CocaLatentError("ratio must be in (0, 1]")
        if max_tokens < 1:
            raise CocaLatentError("max_tokens must be positive")
        self.ratio = ratio
        self.max_tokens = max_tokens

    def compress(self, text: str, *, tool: str, step: int) -> LatentObservation:
        """Compress one observation into latent tokens."""

        words = _WHITESPACE.findall(text)
        if not words:
            raise CocaLatentError("observation text must not be empty")
        source_tokens = len(words)
        target = max(1, math.ceil(source_tokens * self.ratio))
        target = min(target, self.max_tokens, source_tokens)
        stride = max(1, source_tokens // target)
        selected = words[::stride][:target]
        weight = 1.0 / len(selected)
        tokens = tuple(
            LatentToken(_fnv1a(f"{tool}\0{step}\0{word}"), weight)
            for word in selected
        )
        return LatentObservation(
            tool=tool,
            step=step,
            latent_tokens=tokens,
            source_tokens=source_tokens,
            source_chars=len(text),
        )

    def compress_many(
        self, observations: Iterable[tuple[str, int, str]]
    ) -> tuple[LatentObservation, ...]:
        """Compress `(tool, step, text)` tuples preserving order."""

        return tuple(
            self.compress(text, tool=tool, step=step)
            for tool, step, text in observations
        )

    @staticmethod
    def kl_divergence(before: ActionDistribution, after: ActionDistribution) -> float:
        """KL(before || after), treating missing actions as tiny mass."""

        actions = sorted(set(before.actions) | set(after.actions))
        epsilon = 1e-12
        total = 0.0
        for action in actions:
            p = before.probability(action)
            q = after.probability(action)
            if p <= 0.0:
                continue
            total += p * math.log(p / max(q, epsilon))
        return max(0.0, total)

    def preserves_action(
        self,
        before: ActionDistribution,
        after: ActionDistribution,
        *,
        max_kl: float = 0.05,
    ) -> bool:
        """Whether compression preserves the next-action distribution."""

        if not math.isfinite(max_kl) or max_kl < 0.0:
            raise CocaLatentError("max_kl must be finite and non-negative")
        return self.kl_divergence(before, after) <= max_kl
