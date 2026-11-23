from __future__ import annotations

import pytest
from ultimate_coders.architecture.anys1_router import (
    ANYS1_ROUTER_ENV,
    AnyS1Router,
    DecisionOption,
    anys1_router_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ANYS1_ROUTER_ENV, raising=False)
    assert not anys1_router_enabled()
    monkeypatch.setenv(ANYS1_ROUTER_ENV, "yes")
    assert anys1_router_enabled()


def test_empty_options_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        AnyS1Router().route([])


def test_high_confidence_stays_on_system_one() -> None:
    decision = AnyS1Router(0.8).route([DecisionOption("edit", 0.9)])
    assert decision.system == 1
    assert not decision.escalated
    assert decision.action == "edit"


def test_low_confidence_escalates_to_system_two() -> None:
    decision = AnyS1Router(0.8).route([DecisionOption("edit", 0.4)])
    assert decision.system == 2
    assert decision.escalated
    assert decision.reason == "below_confidence_threshold"


def test_tie_uses_lower_cost_then_action() -> None:
    router = AnyS1Router(0.8)
    assert router.route(
        [DecisionOption("b", 0.9, 2.0), DecisionOption("a", 0.9, 1.0)]
    ).action == "a"
    assert router.route(
        [DecisionOption("b", 0.9, 1.0), DecisionOption("a", 0.9, 1.0)]
    ).action == "a"


def test_stats_report_accuracy_and_calibration_error() -> None:
    router = AnyS1Router()
    router.observe(0.9, True)
    router.observe(0.8, True)
    router.observe(0.2, False)
    stats = router.stats()
    assert stats.observations == 3
    assert stats.accuracy == pytest.approx(2 / 3)
    assert stats.mean_confidence == pytest.approx((0.9 + 0.8 + 0.2) / 3)
    assert stats.calibration_error == pytest.approx((0.1 + 0.2 + 0.2) / 3)
    assert stats.to_dict()["observations"] == 3


def test_three_misses_pull_confidence_below_threshold() -> None:
    router = AnyS1Router(0.75)
    for _ in range(3):
        router.observe(0.9, False)
    assert router.calibrate(0.9) == pytest.approx(0.6)
    assert router.route([DecisionOption("edit", 0.9)]).escalated


def test_calibration_is_identity_before_three_observations() -> None:
    router = AnyS1Router()
    router.observe(0.9, False)
    assert router.calibrate(0.9) == 0.9


def test_invalid_option_and_observation_are_rejected() -> None:
    with pytest.raises(ValueError, match="confidence"):
        DecisionOption("edit", 1.2)
    with pytest.raises(ValueError, match="action"):
        DecisionOption("", 0.5)
    with pytest.raises(ValueError, match="confidence"):
        AnyS1Router().observe(1.5, True)


def test_reset_clears_calibration() -> None:
    router = AnyS1Router()
    router.observe(0.9, True)
    router.reset()
    assert router.observations == 0
    assert router.stats().to_dict() == {
        "observations": 0,
        "accuracy": 0.0,
        "mean_confidence": 0.0,
        "calibration_error": 0.0,
    }
