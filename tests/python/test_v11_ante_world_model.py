"""Tests for the v11 ANTE world model (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.ante_world_model import (
    UC_ANTE_WORLD_MODEL,
    ActionPrediction,
    AnteWorldModel,
    CandidateAction,
    ConfidenceDecision,
    PredictionOutcome,
    ante_world_model_enabled,
    bapo_advantage,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_ANTE_WORLD_MODEL, raising=False)
    assert ante_world_model_enabled() is False
    monkeypatch.setenv(UC_ANTE_WORLD_MODEL, "yes")
    assert ante_world_model_enabled() is True


def test_penalty_gamma_formula() -> None:
    model = AnteWorldModel(target_accuracy=0.8, penalty_alpha=1.0)
    assert model.penalty_gamma == pytest.approx(4.0)


def test_trust_decision_uses_target_accuracy() -> None:
    model = AnteWorldModel(target_accuracy=0.75)
    assert model.should_trust(0.8) is True
    assert model.should_trust(0.75) is False


def test_wrong_trusted_prediction_is_penalized() -> None:
    model = AnteWorldModel(target_accuracy=0.8)
    model.register(
        "a1",
        ActionPrediction(
            predicted_exit_code=0,
            predicted_observation_digest="ok",
            confidence_decision=ConfidenceDecision.TRUST,
        ),
    )
    evaluation = model.observe("a1", PredictionOutcome(exit_code=1, observation_digest="bad"))
    assert evaluation.reward == 0.0
    assert evaluation.utility == pytest.approx(-4.0)
    assert evaluation.trusted is True


def test_correct_prediction_rewards_and_accuracy() -> None:
    model = AnteWorldModel(target_accuracy=0.8)
    model.register(
        "a1",
        ActionPrediction(0, "ok", ConfidenceDecision.TRUST),
    )
    evaluation = model.observe("a1", PredictionOutcome(0, "ok"))
    assert evaluation.correct is True
    assert evaluation.utility == 1.0
    assert model.estimate_accuracy() == 1.0


def test_ante_tto_ranks_trusted_candidates() -> None:
    model = AnteWorldModel()
    candidates = [
        CandidateAction("trusted", 10.0, ConfidenceDecision.TRUST),
        CandidateAction("abstain", 99.0, ConfidenceDecision.ABSTAIN),
    ]
    ranked = model.rank_candidates(candidates)
    assert [candidate.candidate_id for candidate in ranked] == ["trusted", "abstain"]


def test_bapo_advantage_anchors_prediction_reward() -> None:
    assert bapo_advantage(1.0, 1.0, coefficient=0.2) == pytest.approx(1.2)
    assert bapo_advantage(1.0, 0.0, coefficient=0.2) == pytest.approx(0.8)
