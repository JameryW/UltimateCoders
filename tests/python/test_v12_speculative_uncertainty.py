"""Tests for the v12 Speculative Uncertainty veto gate (opt-in, default-off)."""

from __future__ import annotations

import math

import pytest
from ultimate_coders.agent.speculative_uncertainty import (
    UC_SPECULATIVE_UNCERTAINTY,
    SpanKind,
    SpanLikelihood,
    SpeculativeUncertaintyGate,
    UncertaintyFeatures,
    assess_uncertainty,
    speculative_uncertainty_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_SPECULATIVE_UNCERTAINTY, raising=False)
    assert speculative_uncertainty_enabled() is False
    monkeypatch.setenv(UC_SPECULATIVE_UNCERTAINTY, "TRUE")
    assert speculative_uncertainty_enabled() is True


def test_span_likelihood_normalizes_and_exposes_surprise() -> None:
    span = SpanLikelihood(SpanKind.ACTION, 12, 0.9)
    assert span.mean_logprob == 0.0
    assert span.surprise == 0.0
    with pytest.raises(ValueError, match="finite"):
        SpanLikelihood(SpanKind.ACTION, 1, math.inf)


def test_confident_action_is_allowed() -> None:
    features = UncertaintyFeatures(
        reasoning=SpanLikelihood(SpanKind.REASONING, 20, -0.02),
        action=SpanLikelihood(SpanKind.ACTION, 8, -0.01),
        stage="editing",
    )
    assessment = assess_uncertainty(features)
    assert assessment.veto is False
    assert assessment.risk < 0.6


def test_high_action_surprise_is_vetoed() -> None:
    features = UncertaintyFeatures(
        reasoning=SpanLikelihood(SpanKind.REASONING, 20, -0.02),
        action=SpanLikelihood(SpanKind.ACTION, 8, -2.0),
        stage="editing",
    )
    assessment = assess_uncertainty(features)
    assert assessment.veto is True
    assert assessment.risk >= 0.6


def test_stage_weights_discount_confident_actions_before_editing() -> None:
    features = UncertaintyFeatures(
        reasoning=SpanLikelihood(SpanKind.REASONING, 20, -2.0),
        action=SpanLikelihood(SpanKind.ACTION, 8, 0.0),
        stage="editing",
    )
    editing = assess_uncertainty(features)
    assert editing.veto is False
    assert editing.risk < 0.6

    localization = assess_uncertainty(
        UncertaintyFeatures(features.reasoning, features.action, "verification")
    )
    assert localization.veto is True
    assert localization.weights == (0.5, 0.5)


def test_empty_action_span_cannot_be_vetoed() -> None:
    features = UncertaintyFeatures(
        reasoning=SpanLikelihood(SpanKind.REASONING, 20, -2.5),
        action=SpanLikelihood(SpanKind.ACTION, 0, -2.5),
        stage="verification",
    )
    assessment = assess_uncertainty(features)
    assert assessment.veto is False
    assert "nothing to veto" in assessment.reason


def test_custom_threshold_and_probability_bounds() -> None:
    features = UncertaintyFeatures(
        reasoning=SpanLikelihood(SpanKind.REASONING, 4, -0.8),
        action=SpanLikelihood(SpanKind.ACTION, 4, -0.8),
    )
    gate = SpeculativeUncertaintyGate(threshold=0.95)
    assessment = gate.assess(features)
    assert assessment.veto is False
    assert 0.0 <= assessment.calibrated_probability <= 1.0
    assert assessment.to_dict()["veto"] is False
