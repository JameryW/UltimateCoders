"""Tests for the V19 PerfAgent optimization loop (#76)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.perf_agent import (
    PERF_AGENT_ENV,
    OptimizationCandidate,
    PerfAgent,
    ProfileSample,
    perf_agent_enabled,
)


def sample(function: str, samples: int, share: float) -> ProfileSample:
    return ProfileSample(function, samples, share)


def candidate(
    id: str,
    *,
    speedup: float = 1.2,
    tests_passed: bool = True,
    before: float = 0.8,
    after: float = 0.4,
) -> OptimizationCandidate:
    return OptimizationCandidate(id, f"patch-{id}", speedup, tests_passed, before, after)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PERF_AGENT_ENV, raising=False)
    assert not perf_agent_enabled()
    monkeypatch.setenv(PERF_AGENT_ENV, "yes")
    assert perf_agent_enabled()


def test_hotspots_rank_by_weighted_samples() -> None:
    agent = PerfAgent()
    ranked = agent.hotspots(
        [
            sample("cold", 100, 0.1),
            sample("hot", 50, 0.9),
            sample("medium", 80, 0.5),
        ]
    )
    assert [item.function for item in ranked] == ["hot", "medium", "cold"]


def test_failed_functional_tests_are_rejected() -> None:
    attempt = PerfAgent().evaluate(candidate("a", tests_passed=False))
    assert attempt.accepted is False
    assert "functional" in attempt.reason


def test_speedup_must_beat_minimum_and_current_best() -> None:
    agent = PerfAgent(min_speedup=1.05)
    assert not agent.evaluate(candidate("a", speedup=1.04)).accepted
    assert not agent.evaluate(candidate("b", speedup=1.2), current_speedup=1.3).accepted
    assert agent.evaluate(candidate("c", speedup=1.3), current_speedup=1.2).accepted


def test_hot_stack_share_must_decrease() -> None:
    attempt = PerfAgent().evaluate(candidate("a", before=0.4, after=0.4))
    assert attempt.accepted is False
    assert "hot stack" in attempt.reason


def test_valid_candidate_is_accepted() -> None:
    attempt = PerfAgent().evaluate(candidate("a"))
    assert attempt.accepted is True
    assert attempt.reason == "accepted"


def test_optimize_enforces_monotone_speedup_and_max_rounds() -> None:
    report = PerfAgent(max_rounds=3).optimize(
        [
            candidate("a", speedup=1.2, before=0.8, after=0.5),
            candidate("b", speedup=1.1, before=0.8, after=0.3),
            candidate("c", speedup=1.3, before=0.5, after=0.2),
            candidate("d", speedup=2.0, before=0.2, after=0.1),
        ],
        hotspots=[sample("hot", 10, 0.9)],
    )
    assert report.accepted == ("a", "c")
    assert report.best_speedup == 1.3
    assert report.hot_functions == ("hot",)
    assert len(report.attempts) == 3


def test_report_to_dict_is_serializable() -> None:
    report = PerfAgent(max_rounds=1).optimize([candidate("a")])
    payload = report.to_dict()
    assert payload["accepted"] == ["a"]
    assert payload["best_speedup"] == 1.2
    assert isinstance(payload["attempts"], list)
