"""Tests for the v12 CER prefix reward predictor (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.cer_predictor import (
    UC_CER_PREDICTOR,
    CerPredictor,
    HistoricalTask,
    PrefixFeatures,
    Stage,
    cer_predictor_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_CER_PREDICTOR, raising=False)
    assert cer_predictor_enabled() is False
    monkeypatch.setenv(UC_CER_PREDICTOR, "1")
    assert cer_predictor_enabled() is True


def test_rubric_weights_sum_to_one_and_relevance_adapts_threshold() -> None:
    history = [
        HistoricalTask("t1", ("cache", "sql"), resolved=True),
        HistoricalTask("t2", ("cache",), resolved=False),
    ]
    rubric = CerPredictor(history).build_rubric(("cache", "sql"))
    assert sum(item.weight for item in rubric.items) == pytest.approx(1.0)
    assert rubric.weight_of(Stage.VERIFICATION) > 0
    cold = CerPredictor().build_rubric(("cache", "sql"))
    assert rubric.prune_threshold < cold.prune_threshold
    assert rubric.relevance > cold.relevance


def test_good_prefix_predicts_higher_reward_than_bad_prefix() -> None:
    predictor = CerPredictor()
    good = PrefixFeatures(
        step=20,
        stage=Stage.VERIFICATION,
        tests_passed=5,
        tests_failed=0,
        required_files_touched=2,
        required_files_total=2,
        verification_seen=True,
        unresolved_errors=0,
        progress_ratio=1.0,
    )
    bad = PrefixFeatures(
        step=20,
        stage=Stage.LOCALIZATION,
        tests_passed=0,
        tests_failed=3,
        required_files_touched=0,
        required_files_total=2,
        verification_seen=False,
        unresolved_errors=2,
        progress_ratio=0.1,
    )
    good_prediction = predictor.predict(good, milestone=10)
    bad_prediction = predictor.predict(bad, milestone=10)
    assert good_prediction.reward > bad_prediction.reward
    assert bad_prediction.prune is True
    assert good_prediction.prune is False


def test_pruning_waits_for_the_milestone() -> None:
    predictor = CerPredictor()
    features = PrefixFeatures(
        step=9,
        tests_passed=0,
        tests_failed=2,
        required_files_touched=0,
        required_files_total=2,
        progress_ratio=0.0,
    )
    assert predictor.predict(features, milestone=10).prune is False
    assert predictor.predict(features, milestone=9).prune is True


def test_a_passing_test_always_prevents_early_termination() -> None:
    predictor = CerPredictor()
    features = PrefixFeatures(
        step=30,
        tests_passed=1,
        tests_failed=9,
        required_files_touched=1,
        required_files_total=2,
        progress_ratio=0.2,
    )
    prediction = predictor.predict(features, milestone=10)
    assert prediction.prune is False
    assert "does not justify" in prediction.reason


def test_milestone_scores_return_one_prediction_per_milestone() -> None:
    predictor = CerPredictor()
    features = PrefixFeatures(step=20, tests_passed=2, required_files_total=2)
    scores = predictor.milestone_scores(features, milestones=(10, 20))
    assert len(scores) == 2
    assert scores[0].rubric.milestone == 10
    assert scores[1].rubric.milestone == 20
