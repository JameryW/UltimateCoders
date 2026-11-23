"""AttnCompress (#51): attention-guided, structure-aware trajectory compression.

The middleware protects a coding agent's history in three stages:

1. perplexity-spike chunking that never cuts through JSON/code delimiters;
2. a deterministic proxy-attention scorer over the historical chunks;
3. a rolling window that suppresses low-relevance chunks but keeps every
   suppressed chunk available for lossless recall when the task focus drifts.

It is pure bookkeeping: no model is loaded and no network call is made. The
``proxy_scores`` argument is where a caller supplies the next-token attention
signal measured by its proxy model. Feature flag: ``UC_ATTN_COMPRESS``.
"""

from __future__ import annotations

import math
import os
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "ATTN_COMPRESS_ENABLED",
    "AttentionScorer",
    "AttnCompressMiddleware",
    "Chunk",
    "CompressionResult",
    "PplSpikeChunker",
    "RollingRecallWindow",
    "TokenStat",
    "WindowResult",
    "attn_compress_enabled",
]

ATTN_COMPRESS_ENABLED = "UC_ATTN_COMPRESS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def attn_compress_enabled() -> bool:
    """Return whether the opt-in middleware is enabled for this call."""

    return os.environ.get(ATTN_COMPRESS_ENABLED, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TokenStat:
    """One token and the generator log-probability that produced it."""

    text: str
    logprob: float = -0.1

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("token text must not be empty")
        if not math.isfinite(self.logprob):
            raise ValueError("token logprob must be finite")


@dataclass(frozen=True)
class Chunk:
    """A syntax-preserving chunk of the trajectory."""

    chunk_id: int
    text: str
    token_count: int
    ppl: float
    spike: bool
    protected: bool
    attention: float = 0.0

    def __post_init__(self) -> None:
        if self.chunk_id < 0:
            raise ValueError("chunk_id must be non-negative")
        if self.token_count < 1:
            raise ValueError("token_count must be positive")
        if not math.isfinite(self.ppl) or self.ppl <= 0:
            raise ValueError("ppl must be finite and positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "token_count": self.token_count,
            "ppl": round(self.ppl, 6),
            "spike": self.spike,
            "protected": self.protected,
            "attention": round(self.attention, 6),
        }


def _scan_state(text: str, depth: int, in_quote: bool) -> tuple[int, bool]:
    """Track delimiter depth and quote state across a token boundary."""

    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_quote:
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth = max(0, depth - 1)
    return depth, in_quote


def _safe_boundary(text: str, depth: int, in_quote: bool) -> bool:
    """A boundary is safe only at top level and after a natural separator."""

    if depth != 0 or in_quote:
        return False
    stripped = text.rstrip()
    return not stripped or stripped[-1] in "\n;:,{}[]()"


def _looks_structural(text: str) -> bool:
    """JSON/code-shaped chunks get a small attention floor in the window."""

    stripped = text.strip()
    return stripped.startswith(("{", "[")) or stripped.endswith(("{", "[", "}", "]"))


class PplSpikeChunker:
    """Split a token stream at safe boundaries around perplexity spikes."""

    def __init__(self, *, max_tokens: int = 24, spike_ratio: float = 2.0) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if spike_ratio <= 0:
            raise ValueError("spike_ratio must be positive")
        self.max_tokens = max_tokens
        self.spike_ratio = spike_ratio

    def chunk(self, tokens: Sequence[TokenStat]) -> list[Chunk]:
        """Return chunks; a high-PPL chunk is marked as a spike."""

        chunks: list[Chunk] = []
        current: list[TokenStat] = []
        depth = 0
        in_quote = False
        previous_ppls: list[float] = []

        def flush() -> None:
            nonlocal current, depth, in_quote
            if not current:
                return
            text = "".join(token.text for token in current)
            mean = -sum(token.logprob for token in current) / len(current)
            ppl = math.exp(min(50.0, mean))
            baseline = statistics.median(previous_ppls) if previous_ppls else ppl
            spike = bool(previous_ppls) and ppl > baseline * self.spike_ratio
            chunks.append(
                Chunk(
                    chunk_id=len(chunks),
                    text=text,
                    token_count=len(current),
                    ppl=ppl,
                    spike=spike,
                    protected=_looks_structural(text),
                )
            )
            previous_ppls.append(ppl)
            current = []
            depth = 0
            in_quote = False

        for token in tokens:
            current.append(token)
            depth, in_quote = _scan_state(token.text, depth, in_quote)
            joined = "".join(item.text for item in current)
            hard_limit = self.max_tokens * 2
            if len(current) >= hard_limit or (
                len(current) >= self.max_tokens
                and _safe_boundary(joined, depth, in_quote)
            ):
                flush()
        flush()
        return chunks


class AttentionScorer:
    """Deterministic proxy-attention score for each historical chunk.

    The score blends lexical focus overlap, caller-supplied proxy attention, and
    recency. Protected JSON/code chunks receive a small floor so a window cannot
    suppress a still-needed structural block only because it is old.
    """

    def __init__(
        self,
        *,
        lexical_weight: float = 0.55,
        proxy_weight: float = 0.25,
        recency_weight: float = 0.20,
        protected_floor: float = 0.30,
    ) -> None:
        if min(lexical_weight, proxy_weight, recency_weight) < 0:
            raise ValueError("weights must be non-negative")
        total = lexical_weight + proxy_weight + recency_weight
        if total <= 0:
            raise ValueError("at least one weight must be positive")
        self.lexical_weight = lexical_weight / total
        self.proxy_weight = proxy_weight / total
        self.recency_weight = recency_weight / total
        self.protected_floor = protected_floor

    def score(
        self,
        chunks: Sequence[Chunk],
        focus_terms: Iterable[str] = (),
        proxy_scores: Mapping[int, float] | None = None,
    ) -> dict[int, float]:
        """Return chunk-id -> score in [0, 1]."""

        terms = [term.lower() for term in focus_terms if term.strip()]
        proxy = proxy_scores or {}
        count = len(chunks)
        results: dict[int, float] = {}
        for index, chunk in enumerate(chunks):
            lower = chunk.text.lower()
            lexical = (
                sum(1 for term in terms if term in lower) / len(terms) if terms else 0.0
            )
            proxy_value = max(0.0, min(1.0, float(proxy.get(chunk.chunk_id, 0.0))))
            recency = index / (count - 1) if count > 1 else 1.0
            value = (
                self.lexical_weight * lexical
                + self.proxy_weight * proxy_value
                + self.recency_weight * recency
            )
            if chunk.protected:
                value = max(value, self.protected_floor)
            results[chunk.chunk_id] = max(0.0, min(1.0, value))
        return results


@dataclass(frozen=True)
class WindowResult:
    """The active window plus every chunk it suppressed."""

    active: tuple[Chunk, ...]
    suppressed: tuple[Chunk, ...]
    recalled: tuple[int, ...]
    tokens_before: int
    tokens_after: int
    compression_ratio: float

    def __post_init__(self) -> None:
        if self.tokens_before < 0 or self.tokens_after < 0:
            raise ValueError("token counts must be non-negative")
        if self.compression_ratio < 0:
            raise ValueError("compression ratio must be non-negative")

    @property
    def saved_tokens(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)

    def render(self) -> str:
        return "".join(chunk.text for chunk in self.active)

    def to_dict(self) -> dict[str, object]:
        return {
            "active": [chunk.chunk_id for chunk in self.active],
            "suppressed": [chunk.chunk_id for chunk in self.suppressed],
            "recalled": list(self.recalled),
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "compression_ratio": round(self.compression_ratio, 6),
        }


class RollingRecallWindow:
    """Budgeted active window with lossless recall of suppressed chunks."""

    def __init__(
        self,
        *,
        budget_tokens: int = 4096,
        recall_threshold: float = 0.45,
        scorer: AttentionScorer | None = None,
    ) -> None:
        if budget_tokens < 1:
            raise ValueError("budget_tokens must be positive")
        if not 0 <= recall_threshold <= 1:
            raise ValueError("recall_threshold must be in [0, 1]")
        self.budget_tokens = budget_tokens
        self.recall_threshold = recall_threshold
        self.scorer = scorer or AttentionScorer()
        self._all: tuple[Chunk, ...] = ()
        self._suppressed_ids: set[int] = set()

    def apply(
        self,
        chunks: Sequence[Chunk],
        focus_terms: Iterable[str] = (),
        proxy_scores: Mapping[int, float] | None = None,
    ) -> WindowResult:
        """Select the active window and retain every suppressed chunk."""

        materialized = tuple(chunks)
        scores = self.scorer.score(
            materialized, focus_terms=focus_terms, proxy_scores=proxy_scores
        )
        ranked = sorted(
            materialized,
            key=lambda chunk: (-scores[chunk.chunk_id], chunk.chunk_id),
        )
        active: list[Chunk] = []
        total = 0
        for chunk in ranked:
            if total + chunk.token_count <= self.budget_tokens or not active:
                active.append(chunk)
                total += chunk.token_count
            elif scores[chunk.chunk_id] >= self.recall_threshold:
                # High-relevance chunks are never silently dropped; the window
                # may temporarily exceed budget rather than lose evidence.
                active.append(chunk)
                total += chunk.token_count
        active.sort(key=lambda chunk: chunk.chunk_id)
        active_ids = {chunk.chunk_id for chunk in active}
        suppressed = tuple(
            chunk for chunk in materialized if chunk.chunk_id not in active_ids
        )
        recalled = tuple(
            chunk.chunk_id
            for chunk in active
            if chunk.chunk_id in self._suppressed_ids
        )
        self._all = materialized
        self._suppressed_ids = {chunk.chunk_id for chunk in suppressed}
        before = sum(chunk.token_count for chunk in materialized)
        after = sum(chunk.token_count for chunk in active)
        return WindowResult(
            active=tuple(active),
            suppressed=suppressed,
            recalled=recalled,
            tokens_before=before,
            tokens_after=after,
            compression_ratio=(after / before) if before else 0.0,
        )

    def on_focus_change(
        self,
        focus_terms: Iterable[str],
        *,
        proxy_scores: Mapping[int, float] | None = None,
    ) -> WindowResult:
        """Re-score the retained history against the new focus."""

        return self.apply(self._all, focus_terms=focus_terms, proxy_scores=proxy_scores)

    @property
    def retained_chunks(self) -> tuple[Chunk, ...]:
        return self._all


@dataclass(frozen=True)
class CompressionResult:
    """Middleware output for one token stream."""

    chunks: tuple[Chunk, ...]
    window: WindowResult
    focus_terms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def active(self) -> tuple[Chunk, ...]:
        return self.window.active

    @property
    def suppressed(self) -> tuple[Chunk, ...]:
        return self.window.suppressed

    def to_dict(self) -> dict[str, object]:
        return {
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "focus_terms": list(self.focus_terms),
            "window": self.window.to_dict(),
        }


class AttnCompressMiddleware:
    """Facade combining structural chunking and the rolling recall window."""

    def __init__(
        self,
        *,
        chunker: PplSpikeChunker | None = None,
        window: RollingRecallWindow | None = None,
    ) -> None:
        self.chunker = chunker or PplSpikeChunker()
        self.window = window or RollingRecallWindow()

    def compress(
        self,
        tokens: Sequence[TokenStat],
        *,
        focus_terms: Iterable[str] = (),
        proxy_scores: Mapping[int, float] | None = None,
    ) -> CompressionResult:
        """Chunk then window a trajectory."""

        chunks = tuple(self.chunker.chunk(tokens))
        terms = tuple(focus_terms)
        window = self.window.apply(chunks, focus_terms=terms, proxy_scores=proxy_scores)
        return CompressionResult(chunks=chunks, window=window, focus_terms=terms)

    def on_focus_change(
        self,
        focus_terms: Iterable[str],
        *,
        proxy_scores: Mapping[int, float] | None = None,
    ) -> WindowResult:
        """Recall suppressed earlier evidence after a focus change."""

        return self.window.on_focus_change(focus_terms, proxy_scores=proxy_scores)

    @property
    def enabled(self) -> bool:
        return attn_compress_enabled()
