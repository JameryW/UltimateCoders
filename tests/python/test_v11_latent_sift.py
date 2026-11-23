"""Tests for the v11 LatentSift candidate filter (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.latent_sift import (
    UC_LATENT_SIFT,
    ChannelTrace,
    ExperienceBank,
    LatentCandidate,
    LatentSiftFilter,
    latent_sift_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_LATENT_SIFT, raising=False)
    assert latent_sift_enabled() is False
    monkeypatch.setenv(UC_LATENT_SIFT, "true")
    assert latent_sift_enabled() is True


def _candidate(candidate_id: str, value: float) -> LatentCandidate:
    trace = ChannelTrace(
        reasoning=((value, value),),
        observation=((value, value),),
        function=((value, value),),
    )
    return LatentCandidate(candidate_id=candidate_id, trace=trace)


def test_positive_margin_ranks_first() -> None:
    bank = ExperienceBank(
        positive={channel: [(0.0, 0.0)] for channel in ("reasoning", "observation", "function")},
        negative={channel: [(10.0, 10.0)] for channel in ("reasoning", "observation", "function")},
    )
    report = LatentSiftFilter(bank, bank, top_k=1).score(
        [_candidate("near_positive", 0.0), _candidate("near_negative", 10.0)]
    )
    assert report.selected == ("near_positive",)
    expected = 200**0.5
    assert report.scores[0].channel_margins["reasoning"] == pytest.approx(expected)
    assert report.scores[1].channel_margins["reasoning"] == pytest.approx(-expected)


def test_time_weighting_favours_later_steps() -> None:
    trace = ChannelTrace(reasoning=((0.0, 0.0), (10.0, 10.0)))
    pooled = trace.time_weighted_pool("reasoning")
    assert pooled[0] == pytest.approx(20.0 / 3.0)


def test_linear_scorer_can_break_margin_ties() -> None:
    bank = ExperienceBank(positive={"reasoning": [(0.0,)]}, negative={"reasoning": [(10.0,)]})
    left = LatentCandidate("a", ChannelTrace(reasoning=((0.0,),)))
    right = LatentCandidate("b", ChannelTrace(reasoning=((0.0,),)))
    report = LatentSiftFilter(bank, bank, top_k=1).score(
        [left, right], {"a": 0.0, "b": 5.0}
    )
    assert report.selected == ("b",)


def test_empty_channels_are_tolerated() -> None:
    bank = ExperienceBank(positive={"reasoning": [(0.0,)]}, negative={"reasoning": [(1.0,)]})
    candidate = LatentCandidate("a", ChannelTrace())
    report = LatentSiftFilter(bank, bank, top_k=1).score([candidate])
    assert report.selected == ("a",)
    assert report.scores[0].channel_margins == {}


def test_report_retention_and_dict() -> None:
    bank = ExperienceBank(positive={"reasoning": [(0.0,)]}, negative={"reasoning": [(1.0,)]})
    candidates = [_candidate(f"c{index}", float(index)) for index in range(4)]
    report = LatentSiftFilter(bank, bank, top_k=2).score(candidates)
    assert report.oracle_retention == pytest.approx(0.5)
    assert len(report.dropped) == 2
    assert report.to_dict()["top_k"] == 2
