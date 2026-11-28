"""Tests for focus resumption and behavioral fingerprints (#135)."""

from __future__ import annotations

import pytest
from ultimate_coders.architecture.focus_resumption import (
    FOCUS_RESUMPTION_ENV,
    BehaviorEvent,
    BehaviorFingerprint,
    BehaviorPhase,
    FocusResumption,
    TaskStateCard,
    behavior_fingerprint,
    focus_resumption_enabled,
)


def card(task_id: str = "t1", **overrides: object) -> TaskStateCard:
    values = {
        "task_id": task_id,
        "branch": f"uc/{task_id}",
        "issue": "cache invalidation fails",
        "goals": ("invalidate cache",),
        "files": ("src/cache.py",),
        "decisions": ("keep API stable",),
        "open_steps": ("run tests",),
    }
    values.update(overrides)
    return TaskStateCard(**values)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FOCUS_RESUMPTION_ENV, raising=False)
    assert not focus_resumption_enabled()
    monkeypatch.setenv(FOCUS_RESUMPTION_ENV, "on")
    assert focus_resumption_enabled()


def test_card_summary_and_relevance() -> None:
    state = card()
    assert "src/cache.py" in state.summary()
    assert state.relevance("cache invalidation fails; invalidate cache in src/cache.py") == 3
    assert state.relevance("unrelated") == 0


def test_card_requires_identity_and_goals() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        TaskStateCard(" ", "b", "i", ("g",))
    with pytest.raises(ValueError, match="goals"):
        TaskStateCard("t", "b", "i")


def test_switch_keeps_only_relevant_history() -> None:
    focus = FocusResumption()
    focus.register(card())
    focus.record("t1", "read src/cache.py")
    focus.record("t1", "chat about lunch")
    result = focus.switch("t1")
    assert result.kept_history == ("read src/cache.py",)
    assert result.dropped_history == ("chat about lunch",)
    assert result.to_dict()["task_id"] == "t1"


def test_switch_accepts_an_explicit_history() -> None:
    focus = FocusResumption()
    focus.register(card())
    result = focus.switch("t1", ("cache invalidation fails", "unrelated"))
    assert result.kept_history == ("cache invalidation fails",)
    assert result.dropped_history == ("unrelated",)


def test_unknown_task_and_capacity_are_enforced() -> None:
    focus = FocusResumption(max_cards=1)
    focus.register(card("t1"))
    with pytest.raises(ValueError, match="capacity"):
        focus.register(card("t2"))
    with pytest.raises(KeyError, match="unknown task"):
        focus.record("missing", "line")
    with pytest.raises(KeyError, match="unknown task"):
        focus.switch("missing")


def test_active_tasks_are_sorted_and_record_requires_register() -> None:
    focus = FocusResumption()
    focus.register(card("b"))
    focus.register(card("a"))
    assert focus.active_tasks() == ("a", "b")
    assert focus.card("a").task_id == "a"
    with pytest.raises(KeyError, match="unknown task"):
        FocusResumption().record("missing", "line")


def test_behavior_fingerprint_shares_and_empty_case() -> None:
    events = (
        BehaviorEvent(BehaviorPhase.EXPLORATION, "grep"),
        BehaviorEvent(BehaviorPhase.EXPLORATION, "read"),
        BehaviorEvent(BehaviorPhase.EDITING),
        BehaviorEvent(BehaviorPhase.TESTING),
    )
    fingerprint = behavior_fingerprint(events)
    assert fingerprint.total == 4
    assert fingerprint.shares() == {"exploration": 0.5, "editing": 0.25, "testing": 0.25}
    empty = behavior_fingerprint(())
    assert empty.total == 0
    assert empty.shares() == {"exploration": 0.0, "editing": 0.0, "testing": 0.0}


def test_behavior_drift_and_threshold_validation() -> None:
    base = BehaviorFingerprint(0.6, 0.3, 0.1, 10)
    close = BehaviorFingerprint(0.55, 0.35, 0.10, 10)
    far = BehaviorFingerprint(0.2, 0.7, 0.1, 10)
    assert not base.drift_from(close)
    assert base.drift_from(far)
    with pytest.raises(ValueError, match="threshold"):
        base.drift_from(close, threshold=-0.1)


def test_exploration_events_require_a_tool() -> None:
    with pytest.raises(ValueError, match="tool"):
        BehaviorEvent(BehaviorPhase.EXPLORATION)
    BehaviorEvent(BehaviorPhase.EDITING)
