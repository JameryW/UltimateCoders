"""Tests for the Coca-Cola latent observation compressor (V26 #116)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.coca_latent_ctx import (
    COCA_LATENT_CTX_ENV,
    ActionDistribution,
    CocaLatentCompressor,
    CocaLatentError,
    coca_latent_ctx_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(COCA_LATENT_CTX_ENV, raising=False)
    assert not coca_latent_ctx_enabled()
    monkeypatch.setenv(COCA_LATENT_CTX_ENV, "yes")
    assert coca_latent_ctx_enabled()


def test_compression_reduces_tokens() -> None:
    compressor = CocaLatentCompressor(ratio=0.2)
    observation = compressor.compress(
        "one two three four five six seven eight nine ten", tool="pytest", step=3
    )
    assert observation.source_tokens == 10
    assert observation.token_count == 2
    assert observation.compression_ratio == pytest.approx(0.2)
    assert observation.source_chars == len(
        "one two three four five six seven eight nine ten"
    )


def test_compression_is_deterministic() -> None:
    compressor = CocaLatentCompressor(ratio=0.3)
    first = compressor.compress("alpha beta gamma delta", tool="shell", step=1)
    second = compressor.compress("alpha beta gamma delta", tool="shell", step=1)
    assert first == second


def test_preview_mentions_tool_and_steps() -> None:
    compressor = CocaLatentCompressor(ratio=0.5)
    observation = compressor.compress("alpha beta", tool="grep", step=7)
    assert "grep@7" in observation.preview
    assert "tokens" in observation.preview


def test_invalid_compressor_configuration() -> None:
    with pytest.raises(CocaLatentError):
        CocaLatentCompressor(ratio=0.0)
    with pytest.raises(CocaLatentError):
        CocaLatentCompressor(ratio=1.5)
    with pytest.raises(CocaLatentError):
        CocaLatentCompressor(max_tokens=0)


def test_empty_text_is_rejected() -> None:
    compressor = CocaLatentCompressor()
    with pytest.raises(CocaLatentError):
        compressor.compress("   \n\t", tool="shell", step=0)


def test_kl_is_zero_for_identical_distributions() -> None:
    distribution = ActionDistribution.from_mapping({"read": 0.7, "grep": 0.3})
    assert CocaLatentCompressor.kl_divergence(distribution, distribution) == pytest.approx(0.0)


def test_kl_grows_when_action_mass_moves() -> None:
    before = ActionDistribution.from_mapping({"read": 0.9, "grep": 0.1})
    after = ActionDistribution.from_mapping({"read": 0.1, "grep": 0.9})
    divergence = CocaLatentCompressor.kl_divergence(before, after)
    assert divergence > 0.5
    assert CocaLatentCompressor.kl_divergence(after, before) == pytest.approx(divergence)


def test_action_preservation_threshold() -> None:
    compressor = CocaLatentCompressor(ratio=0.2, max_tokens=8)
    before = ActionDistribution.from_mapping({"edit": 0.8, "test": 0.2})
    close = ActionDistribution.from_mapping({"edit": 0.78, "test": 0.22})
    far = ActionDistribution.from_mapping({"edit": 0.2, "test": 0.8})
    assert compressor.preserves_action(before, close, max_kl=0.1)
    assert not compressor.preserves_action(before, far, max_kl=0.1)
    with pytest.raises(CocaLatentError):
        compressor.preserves_action(before, close, max_kl=-1.0)


def test_compress_many_preserves_order() -> None:
    compressor = CocaLatentCompressor(ratio=0.5)
    observations = compressor.compress_many(
        [("shell", 1, "a b c"), ("pytest", 2, "d e f g")]
    )
    assert [item.tool for item in observations] == ["shell", "pytest"]
    assert [item.step for item in observations] == [1, 2]


def test_invalid_action_distributions_are_rejected() -> None:
    with pytest.raises(CocaLatentError):
        ActionDistribution.from_mapping({})
    with pytest.raises(CocaLatentError):
        ActionDistribution.from_mapping({"edit": -0.2, "test": 1.2})
    with pytest.raises(CocaLatentError):
        ActionDistribution.from_mapping({"edit": 0.2, "test": 0.2})
    with pytest.raises(CocaLatentError):
        ActionDistribution.from_mapping({"": 1.0})
