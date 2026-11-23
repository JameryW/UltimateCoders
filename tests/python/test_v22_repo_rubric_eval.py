"""Tests for V22 ledger item #96 (Dory loops + repository-grounded rubrics)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.repo_rubric_eval import (
    Difficulty,
    DoryLoopPlanner,
    RepoEvidence,
    RepoRubricError,
    RepoRubricEvaluator,
    RepositoryGroundedRubric,
    RubricDimension,
    RubricItem,
)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_REPO_RUBRIC_EVAL", raising=False)
    from ultimate_coders.acceptance.repo_rubric_eval import repo_rubric_eval_enabled

    assert repo_rubric_eval_enabled() is False


def test_loop_plan_scales_with_difficulty() -> None:
    planner = DoryLoopPlanner()
    easy = planner.plan(Difficulty.EASY)
    hard = planner.plan(Difficulty.HARD)
    assert easy.loops == 1
    assert easy.output_token_factor == 1.0
    assert hard.loops == 4
    assert hard.output_token_factor == 0.4


def test_context_window_is_capped() -> None:
    with pytest.raises(RepoRubricError):
        DoryLoopPlanner().plan(Difficulty.HARD, context_tokens=262_145)


def _rubric() -> RepositoryGroundedRubric:
    return RepositoryGroundedRubric(
        (
            RubricItem(
                "correct",
                RubricDimension.CORRECTNESS,
                "fix",
                weight=2.0,
                required_evidence=("src/a.py",),
            ),
            RubricItem(
                "tests", RubricDimension.TESTS, "test", weight=1.0, required_evidence=("test_a",)
            ),
            RubricItem("security", RubricDimension.SECURITY, "guard", weight=1.0),
        ),
        threshold=0.7,
    )


def test_repository_grounded_rubric_scores_evidence() -> None:
    result = _rubric().evaluate(RepoEvidence(changed_paths=("src/a.py",), test_names=("test_a",)))
    assert result.score == 1.0
    assert result.passed is True
    assert result.missing_evidence == ()


def test_missing_evidence_lowers_score_and_is_reported() -> None:
    result = _rubric().evaluate(RepoEvidence(changed_paths=("src/a.py",)))
    assert result.score == 0.75
    assert result.missing_evidence == ("test_a",)


def test_below_threshold_does_not_pass() -> None:
    result = _rubric().evaluate(RepoEvidence())
    assert result.passed is False


def test_evaluator_returns_plan_and_digest() -> None:
    report = RepoRubricEvaluator(_rubric()).evaluate(
        "diff --git a/src/a.py b/src/a.py",
        RepoEvidence(changed_paths=("src/a.py",), test_names=("test_a",)),
        difficulty=Difficulty.MEDIUM,
    )
    assert report.plan.loops == 2
    assert report.patch_digest.startswith("sha256:")
    assert report.to_dict()["rubric"]["passed"] is True


def test_patch_must_not_be_empty() -> None:
    with pytest.raises(RepoRubricError):
        RepoRubricEvaluator(_rubric()).evaluate("   ", RepoEvidence())


def test_empty_rubric_is_rejected() -> None:
    with pytest.raises(RepoRubricError):
        RepositoryGroundedRubric(())
