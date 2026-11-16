"""Tests for the v10 GDPO component-normalised scorer (#21)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.gdpo_scorer import (
    ComponentScores,
    GDPOGroup,
    GDPOWeight,
    gdpo_scorer_enabled,
)


def test_default_weights_sum_to_one() -> None:
    weight = GDPOWeight()
    assert weight.pass_rate == 0.5
    assert weight.f2p_rate == 0.3
    assert weight.p2p_rate == 0.2
    assert weight.total == pytest.approx(1.0)
    assert weight.normalised().total == pytest.approx(1.0)


def test_per_component_means_are_zero() -> None:
    group = GDPOGroup(
        [
            ComponentScores(1.0, 0.2, 0.9),
            ComponentScores(0.0, 0.8, 0.5),
            ComponentScores(0.5, 0.5, 0.7),
        ]
    )
    normalised = group.normalise()
    assert len(normalised) == 3
    for component in ("pass_rate", "f2p_rate", "p2p_rate"):
        values = [getattr(score, component) for score in normalised]
        assert sum(values) / len(values) == pytest.approx(0.0, abs=1e-9)


def test_weighted_batch_norm_is_deterministic_and_renormalises() -> None:
    scores = [
        ComponentScores(1.0, 0.0, 0.5),
        ComponentScores(0.0, 1.0, 0.5),
        ComponentScores(0.5, 0.5, 0.5),
    ]
    first = GDPOGroup(scores).weighted_batch_norm()
    second = GDPOGroup(scores).weighted_batch_norm()
    assert first == second
    assert len(first) == 3
    uniform = GDPOGroup(scores, weight=GDPOWeight(1.0, 1.0, 1.0)).weighted_batch_norm()
    explicit = GDPOGroup(scores).weighted_batch_norm(weight=GDPOWeight(1.0, 1.0, 1.0))
    assert uniform == explicit


def test_empty_group() -> None:
    group = GDPOGroup([])
    assert len(group) == 0
    assert group.normalise() == ()
    assert group.weighted_batch_norm() == ()
    assert group.summary()["count"] == 0


def test_zero_variance_component_normalises_to_zero() -> None:
    group = GDPOGroup(
        [
            ComponentScores(0.5, 0.1, 0.5),
            ComponentScores(0.5, 0.9, 0.5),
        ]
    )
    normalised = group.normalise()
    assert normalised[0].pass_rate == 0.0
    assert normalised[1].p2p_rate == 0.0


def test_mapping_input_and_invalid_weight() -> None:
    group = GDPOGroup([{"pass_rate": 1.0, "f2p_rate": 0.0, "p2p_rate": 1.0}])
    assert group.scores[0].pass_rate == 1.0
    with pytest.raises(ValueError):
        GDPOWeight(0.0, 0.0, 0.0)


def test_flag(monkeypatch) -> None:
    monkeypatch.delenv("UC_GDPO_SCORER", raising=False)
    assert gdpo_scorer_enabled() is False
    monkeypatch.setenv("UC_GDPO_SCORER", "yes")
    assert gdpo_scorer_enabled() is True
    monkeypatch.setenv("UC_GDPO_SCORER", "")
    assert gdpo_scorer_enabled() is False
