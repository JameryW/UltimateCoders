"""Tests for repository-grounded rubrics and collusion auditing (#138)."""

from __future__ import annotations

import math

import pytest
from ultimate_coders.acceptance.repo_rubric_scorer import (
    REPO_RUBRIC_SCORER_ENV,
    CollusionReport,
    DecisionSample,
    DecisionStateMonitor,
    FrozenRubric,
    PatchEvidence,
    RepoGroundedRubricScorer,
    RubricDimension,
    RubricItem,
    repo_rubric_scorer_enabled,
)


def rubric() -> FrozenRubric:
    return FrozenRubric(
        "abc123",
        (
            RubricItem("core", RubricDimension.CORRECTNESS, "core behavior", 2.0, True),
            RubricItem(
                "maintainability",
                RubricDimension.MAINTAINABILITY,
                "symbols preserved",
                1.0,
                True,
            ),
            RubricItem("security", RubricDimension.SECURITY, "no secret leak", 1.0, False),
        ),
    )


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPO_RUBRIC_SCORER_ENV, raising=False)
    assert not repo_rubric_scorer_enabled()
    monkeypatch.setenv(REPO_RUBRIC_SCORER_ENV, "yes")
    assert repo_rubric_scorer_enabled()


def test_rubric_validation() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        FrozenRubric(
            "abc",
            (
                RubricItem("a", RubricDimension.CORRECTNESS, "a"),
                RubricItem("a", RubricDimension.SECURITY, "b"),
            ),
        )
    with pytest.raises(ValueError, match="positive"):
        RubricItem("a", RubricDimension.CORRECTNESS, "a", 0.0)
    with pytest.raises(ValueError, match="item_id"):
        RubricItem(" ", RubricDimension.CORRECTNESS, "a")


def test_full_evidence_passes() -> None:
    scorer = RepoGroundedRubricScorer(rubric())
    score = scorer.score(
        "p1",
        PatchEvidence(
            paths=("src/cache.py",),
            symbols=("Cache.invalidate",),
            tests_passed=10,
            features=frozenset({"security"}),
        ),
    )
    assert score.passed
    assert score.weighted_score == 1.0
    assert score.missing_required == ()
    assert score.to_dict()["weighted_score"] == 1.0


def test_missing_required_evidence_fails_even_with_clean_tests() -> None:
    scorer = RepoGroundedRubricScorer(rubric())
    score = scorer.score("p1", PatchEvidence(tests_passed=5))
    assert not score.passed
    assert "maintainability" in score.missing_required
    assert score.weighted_score < 1.0


def test_low_weighted_score_fails_without_required_items() -> None:
    scorer = RepoGroundedRubricScorer(
        FrozenRubric(
            "abc",
            (
                RubricItem("core", RubricDimension.CORRECTNESS, "core", 1.0),
                RubricItem("maint", RubricDimension.MAINTAINABILITY, "symbols", 1.0),
            ),
        )
    )
    score = scorer.score("p1", PatchEvidence(tests_passed=5))
    assert not score.passed
    assert score.weighted_score == 0.5


def test_failing_tests_never_pass() -> None:
    scorer = RepoGroundedRubricScorer(rubric())
    score = scorer.score(
        "p1",
        PatchEvidence(paths=("a",), symbols=("b",), tests_passed=1, tests_failed=1),
    )
    assert not score.passed


def test_rank_orders_positive_above_negative() -> None:
    scorer = RepoGroundedRubricScorer(rubric())
    report = scorer.rank(
        [PatchEvidence(paths=("a",), symbols=("b",), tests_passed=2)],
        [PatchEvidence()],
    )
    assert report.auroc == 1.0
    assert report.positive == 1
    assert report.negative == 1
    with pytest.raises(ValueError, match="positive and negative"):
        scorer.rank([], [PatchEvidence()])


def test_empty_patch_id_is_rejected() -> None:
    scorer = RepoGroundedRubricScorer(rubric())
    with pytest.raises(ValueError, match="patch_id"):
        scorer.score(" ", PatchEvidence(tests_passed=1))


def test_text_only_collusion_probe_is_weak_but_decision_state_is_not() -> None:
    monitor = DecisionStateMonitor()
    text_samples = (
        DecisionSample("a", (0.0,), True),
        DecisionSample("b", (0.0,), False),
    )
    text = monitor.instruction_probe(text_samples)
    assert isinstance(text, CollusionReport)
    assert text.auroc == 0.5
    assert text.text_only_weak

    state_samples = (
        DecisionSample("c", (5.0,), True),
        DecisionSample("d", (0.0,), False),
    )
    state = monitor.decision_monitor(state_samples)
    assert state.feature_kind.value == "decision_state"
    assert state.auroc == 1.0
    assert not state.text_only_weak


def test_decision_samples_validate_ids_and_features() -> None:
    with pytest.raises(ValueError, match="sample_id"):
        DecisionSample(" ", (1.0,), False)
    with pytest.raises(ValueError, match="features"):
        DecisionSample("a", (), False)
    with pytest.raises(ValueError, match="finite"):
        DecisionSample("a", (math.nan,), False)
    with pytest.raises(ValueError, match="samples"):
        DecisionStateMonitor().decision_monitor(())


def test_patch_evidence_rejects_negative_test_counts() -> None:
    with pytest.raises(ValueError, match="negative"):
        PatchEvidence(tests_failed=-1)
