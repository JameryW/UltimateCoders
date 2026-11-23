"""Tests for ledger item #89 (continuous requirement tracking and retry gate)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.requirement_tracker import (
    FailureTrace,
    RequirementTracker,
    RequirementTrackerError,
    build_retry_plan,
    requirement_tracker_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_REQUIREMENT_TRACKER", raising=False)
    assert not requirement_tracker_enabled()
    monkeypatch.setenv("UC_REQUIREMENT_TRACKER", "true")
    assert requirement_tracker_enabled()


def test_duplicate_requirement_is_rejected() -> None:
    tracker = RequirementTracker()
    tracker.add("r1", "create endpoint")
    with pytest.raises(RequirementTrackerError, match="duplicate"):
        tracker.add("r1", "other")


def test_ask_marks_requirement_clarifying() -> None:
    tracker = RequirementTracker()
    tracker.add("r1", "create endpoint")
    tracker.ask("r1", "which auth mode?", turn=1)
    assert tracker.report().by_status == (("clarifying", 1),)


def test_answer_all_clarifications_moves_to_follow_up() -> None:
    tracker = RequirementTracker()
    tracker.add("r1", "create endpoint")
    clarification = tracker.ask("r1", "which auth mode?", turn=1)
    tracker.answer(clarification.clarification_id, "bearer")
    assert tracker.report().by_status == (("follow_up", 1),)


def test_verify_requires_no_open_clarifications() -> None:
    tracker = RequirementTracker()
    tracker.add("r1", "create endpoint")
    tracker.ask("r1", "which auth mode?", turn=1)
    with pytest.raises(RequirementTrackerError, match="open clarifications"):
        tracker.verify("r1", "tests pass")


def test_verify_marks_requirement_verified() -> None:
    tracker = RequirementTracker()
    tracker.add("r1", "create endpoint")
    tracker.verify("r1", "tests pass")
    assert tracker.report().verified == 1


def test_fail_marks_requirement_failed() -> None:
    tracker = RequirementTracker()
    tracker.add("r1", "create endpoint")
    tracker.fail("r1")
    assert tracker.report().by_status == (("failed", 1),)


def test_pending_questions_are_exposed() -> None:
    tracker = RequirementTracker()
    tracker.add("r1", "create endpoint")
    tracker.ask("r1", "which auth mode?", turn=1)
    assert len(tracker.pending_questions()) == 1


def test_retry_plan_uses_raw_trajectory_and_full_regression() -> None:
    plan = build_retry_plan(FailureTrace("a1", "raw search/edit trace", 2, 5))
    assert plan.raw_trajectory == "raw search/edit trace"
    assert plan.run_full_regression
    assert plan.do_not_summarize
    assert plan.to_dict()["run_full_regression"] is True


def test_retry_plan_rejects_summary_only_retry() -> None:
    with pytest.raises(RequirementTrackerError, match="full regression"):
        build_retry_plan(FailureTrace("a1", "raw", 1, 2), run_full_regression=False)
