"""Tests for AttnCompress middleware (#51)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.attn_compress_middleware import (
    ATTN_COMPRESS_ENABLED,
    AttentionScorer,
    AttnCompressMiddleware,
    Chunk,
    PplSpikeChunker,
    RollingRecallWindow,
    TokenStat,
    attn_compress_enabled,
)


def _chunk(chunk_id: int, text: str, tokens: int = 5, protected: bool = False) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        token_count=tokens,
        ppl=1.0,
        spike=False,
        protected=protected,
    )


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ATTN_COMPRESS_ENABLED, raising=False)
    assert not attn_compress_enabled()
    monkeypatch.setenv(ATTN_COMPRESS_ENABLED, "on")
    assert attn_compress_enabled()


def test_chunker_does_not_split_inside_a_json_block() -> None:
    tokens = [
        TokenStat("def f():\n"),
        TokenStat("    return {"),
        TokenStat('"a": 1,'),
        TokenStat("}\n"),
        TokenStat("def g():\n"),
    ]
    chunks = PplSpikeChunker(max_tokens=2).chunk(tokens)
    assert len(chunks) == 2
    assert chunks[0].text.endswith("}\n")
    assert chunks[0].text.count("{") == chunks[0].text.count("}")
    assert chunks[1].text.startswith("def g")


def test_chunker_marks_a_perplexity_spike() -> None:
    chunks = PplSpikeChunker(max_tokens=1, spike_ratio=2.0).chunk(
        [TokenStat("a;", -0.01), TokenStat("b;", -3.0)]
    )
    assert len(chunks) == 2
    assert not chunks[0].spike
    assert chunks[1].spike
    assert chunks[1].ppl > chunks[0].ppl * 2


def test_chunker_rejects_invalid_options() -> None:
    with pytest.raises(ValueError):
        PplSpikeChunker(max_tokens=0)
    with pytest.raises(ValueError):
        PplSpikeChunker(spike_ratio=0)


def test_attention_prefers_focus_over_recency() -> None:
    scorer = AttentionScorer()
    chunks = [_chunk(0, "cache invalidation"), _chunk(1, "unrelated output")]
    scores = scorer.score(chunks, focus_terms=("cache",))
    assert scores[0] > scores[1]


def test_protected_chunks_get_a_floor() -> None:
    scorer = AttentionScorer()
    chunks = [_chunk(0, "old", protected=True), _chunk(1, "new")]
    scores = scorer.score(chunks)
    assert scores[0] >= 0.30


def test_window_suppresses_low_relevance_chunks() -> None:
    window = RollingRecallWindow(budget_tokens=5)
    chunks = [_chunk(0, "cache"), _chunk(1, "other")]
    result = window.apply(chunks, focus_terms=("other",))
    assert [chunk.chunk_id for chunk in result.active] == [1]
    assert [chunk.chunk_id for chunk in result.suppressed] == [0]
    assert result.tokens_after == 5
    assert result.tokens_before == 10


def test_suppressed_chunk_is_recalled_after_focus_change() -> None:
    window = RollingRecallWindow(budget_tokens=5)
    chunks = [_chunk(0, "cache"), _chunk(1, "other")]
    first = window.apply(chunks, focus_terms=("other",))
    assert [chunk.chunk_id for chunk in first.active] == [1]

    recalled = window.on_focus_change(("cache",))
    assert [chunk.chunk_id for chunk in recalled.active] == [0]
    assert recalled.recalled == (0,)
    assert [chunk.chunk_id for chunk in recalled.suppressed] == [1]


def test_every_chunk_is_retained_even_when_suppressed() -> None:
    window = RollingRecallWindow(budget_tokens=1)
    chunks = [_chunk(0, "a"), _chunk(1, "b"), _chunk(2, "c")]
    result = window.apply(chunks, focus_terms=("a",))
    retained = {chunk.chunk_id for chunk in result.active + result.suppressed}
    assert retained == {0, 1, 2}
    assert result.saved_tokens == result.tokens_before - result.tokens_after


def test_compression_ratio_is_reported() -> None:
    window = RollingRecallWindow(budget_tokens=5)
    result = window.apply([_chunk(0, "a"), _chunk(1, "b")], focus_terms=("b",))
    assert result.compression_ratio == 0.5


def test_empty_stream_is_safe() -> None:
    middleware = AttnCompressMiddleware()
    result = middleware.compress([])
    assert result.chunks == ()
    assert result.window.tokens_before == 0
    assert result.window.compression_ratio == 0.0


def test_middleware_handles_focus_change() -> None:
    middleware = AttnCompressMiddleware(
        chunker=PplSpikeChunker(max_tokens=1),
        window=RollingRecallWindow(budget_tokens=1, recall_threshold=0.9),
    )
    tokens = [TokenStat("cache;", -0.01), TokenStat("other;", -0.01)]
    first = middleware.compress(tokens, focus_terms=("other",))
    assert len(first.chunks) == 2
    assert [chunk.chunk_id for chunk in first.active] == [1]
    second = middleware.on_focus_change(("cache",))
    assert [chunk.chunk_id for chunk in second.active] == [0]
    assert second.recalled == (0,)


def test_window_validates_budget_and_threshold() -> None:
    with pytest.raises(ValueError):
        RollingRecallWindow(budget_tokens=0)
    with pytest.raises(ValueError):
        RollingRecallWindow(recall_threshold=1.1)
