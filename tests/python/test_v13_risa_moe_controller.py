"""Tests for the v13 Risa sparse-MoE routing controller (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.risa_moe_controller import (
    RISA_MOE_ENABLED_ENV,
    AttemptRoute,
    RisaMoEController,
    RouteCandidate,
    RoutePhase,
    cosine_similarity,
    risa_moe_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RISA_MOE_ENABLED_ENV, raising=False)
    assert risa_moe_enabled() is False
    monkeypatch.setenv(RISA_MOE_ENABLED_ENV, "on")
    assert risa_moe_enabled() is True


def test_exploration_prefers_the_route_farthest_from_history() -> None:
    controller = RisaMoEController(history_window=4)
    controller.record(RouteCandidate("h", "localization", (1.0, 0.0)))
    candidates = [
        RouteCandidate("same", "localization", (1.0, 0.0)),
        RouteCandidate("different", "localization", (-1.0, 0.0)),
    ]
    chosen = controller.select(candidates, RoutePhase.EXPLORATION)
    assert chosen is not None
    assert chosen.id == "different"


def test_commitment_prefers_the_same_role_peer() -> None:
    controller = RisaMoEController()
    candidates = [
        RouteCandidate("a", "editing", (1.0, 0.0)),
        RouteCandidate("b", "editing", (0.9, 0.1)),
        RouteCandidate("c", "editing", (-1.0, 0.0)),
        RouteCandidate("other-role", "verification", (-1.0, 0.0)),
    ]
    chosen = controller.select(candidates, RoutePhase.EDITING)
    assert chosen is not None
    assert chosen.id == "b"
    assert controller.commitment_score(candidates[1], candidates) > controller.commitment_score(
        candidates[2], candidates
    )


def test_commitment_without_peers_is_zero() -> None:
    controller = RisaMoEController()
    solo = RouteCandidate("solo", "editing", (0.2, 0.3))
    assert controller.commitment_score(solo, [solo]) == 0.0
    assert controller.select([solo], RoutePhase.EDITING) is solo


def test_history_is_bounded_and_resettable() -> None:
    controller = RisaMoEController(history_window=2)
    for index in range(3):
        controller.record(RouteCandidate(f"r{index}", "loc", (float(index), 0.0)))
    assert len(controller.history) == 2
    controller.reset()
    assert controller.history == ()


def test_arbitration_prefers_the_route_shared_by_the_group() -> None:
    controller = RisaMoEController(concurrent_attempts=3)
    attempts = [
        AttemptRoute("a", (1.0, 0.0), 0.5),
        AttemptRoute("b", (1.0, 0.0), 0.5),
        AttemptRoute("c", (-1.0, 0.0), 0.5),
    ]
    result = controller.arbitrate(attempts)
    assert result.winner is not None
    assert result.winner.attempt_id == "a"
    assert result.scores["a"] > result.scores["c"]
    assert result.consistency["a"] > result.consistency["c"]


def test_low_confidence_breaks_a_route_tie() -> None:
    controller = RisaMoEController()
    result = controller.arbitrate(
        [
            AttemptRoute("confident", (0.3, 0.7), 0.95),
            AttemptRoute("uncertain", (0.3, 0.7), 0.05),
        ]
    )
    assert result.winner is not None
    assert result.winner.attempt_id == "uncertain"


def test_verification_uses_the_commitment_objective() -> None:
    controller = RisaMoEController()
    candidates = [
        RouteCandidate("left", "verification", (1.0, 0.0)),
        RouteCandidate("right", "verification", (-1.0, 0.0)),
    ]
    chosen = controller.select(candidates, RoutePhase.VERIFICATION)
    assert chosen is not None
    assert chosen.id == "left"


def test_empty_candidate_list_returns_none() -> None:
    assert RisaMoEController().select([], RoutePhase.COMMIT) is None
    assert RisaMoEController().arbitrate([]).winner is None


def test_invalid_routes_are_rejected() -> None:
    with pytest.raises(ValueError):
        RouteCandidate("empty", "loc", ())
    with pytest.raises(ValueError):
        RouteCandidate("bad", "loc", (float("nan"), 0.0))
    with pytest.raises(ValueError):
        RouteCandidate("dim", "loc", (1.0, 0.0), decision_route=(1.0,))
    with pytest.raises(ValueError):
        AttemptRoute("a", ())


def test_cosine_similarity_handles_zero_vectors() -> None:
    assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        cosine_similarity((1.0, 0.0), (1.0,))
