"""Tests for the v12 AutoCompact policy (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.auto_compact import (
    UC_AUTO_COMPACT,
    AutoCompactPolicy,
    CompactTrigger,
    SummaryDraft,
    WorkingState,
    auto_compact_enabled,
    build_segments,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_AUTO_COMPACT, raising=False)
    assert auto_compact_enabled() is False
    monkeypatch.setenv(UC_AUTO_COMPACT, "on")
    assert auto_compact_enabled() is True


def test_phase_completion_triggers_compaction() -> None:
    policy = AutoCompactPolicy()
    state = WorkingState(
        step=8,
        phase="localization",
        used_tokens=500,
        files_touched=("src/a.py",),
        proposed_next_action="read src/a.py and inspect the failing function",
        completed_phases=("localization",),
    )
    result = policy.compact(state)
    assert result.accepted is True
    assert result.decision.trigger is CompactTrigger.PHASE_COMPLETE
    assert "src/a.py" in result.summary
    assert result.segments[-1].end_step == 8


def test_budget_pressure_triggers_before_hard_overflow() -> None:
    policy = AutoCompactPolicy(soft_ratio=0.7)
    state = WorkingState(
        step=5,
        phase="editing",
        used_tokens=1500,
        budget_tokens=2000,
        files_touched=("src/b.py",),
        proposed_next_action="read src/b.py once more",
    )
    decision = policy.decide(state)
    assert decision.should_compact is True
    assert decision.trigger is CompactTrigger.BUDGET_PRESSURE


def test_compaction_cooldown_is_enforced() -> None:
    policy = AutoCompactPolicy(min_steps_between_compactions=3)
    state = WorkingState(
        step=4,
        phase="editing",
        used_tokens=1600,
        files_touched=("src/c.py",),
        proposed_next_action="read src/c.py",
    )
    assert policy.compact(state).accepted is True
    too_soon = policy.compact(
        WorkingState(
            step=5,
            phase="editing",
            used_tokens=1700,
            files_touched=("src/c.py",),
            proposed_next_action="read src/c.py again",
        )
    )
    assert too_soon.accepted is False
    assert too_soon.decision.cooldown_remaining == 2
    later = policy.compact(
        WorkingState(
            step=8,
            phase="editing",
            used_tokens=1800,
            files_touched=("src/c.py",),
            proposed_next_action="read src/c.py again",
        )
    )
    assert later.accepted is True


def test_summary_contradiction_is_rejected() -> None:
    policy = AutoCompactPolicy()
    state = WorkingState(
        step=9,
        phase="repair",
        used_tokens=1900,
        unresolved_errors=("SyntaxError in src/a.py",),
        files_touched=("src/a.py",),
        proposed_next_action="finish and declare the task complete",
    )
    result = policy.compact(state)
    assert result.accepted is False
    assert result.audit.consistent is False
    assert "unresolved_errors_with_completion_action" in result.audit.violations
    assert result.summary == ""


def test_consistent_repair_summary_is_accepted() -> None:
    policy = AutoCompactPolicy()
    summary = SummaryDraft(
        body=(
            "# Auto Context Summary\n"
            "SyntaxError in src/a.py is still unresolved.\n"
            "Files touched: src/a.py\n"
            "Next step: read src/a.py and rerun the syntax check."
        ),
        unresolved_errors=("SyntaxError in src/a.py",),
        files_touched=("src/a.py",),
        proposed_next_action="read src/a.py and rerun the syntax check",
    )
    state = WorkingState(
        step=9,
        phase="repair",
        used_tokens=1900,
        unresolved_errors=("SyntaxError in src/a.py",),
        files_touched=("src/a.py",),
        proposed_next_action="read src/a.py and rerun the syntax check",
    )
    result = policy.compact(state, summary)
    assert result.accepted is True
    assert result.audit.consistent is True


def test_manual_compaction_ignores_only_budget() -> None:
    policy = AutoCompactPolicy()
    state = WorkingState(
        step=2,
        phase="localization",
        used_tokens=10,
        files_touched=("src/d.py",),
        proposed_next_action="read src/d.py",
    )
    assert policy.compact(state, trigger=CompactTrigger.MANUAL).accepted is True


def test_segments_are_monotonic_and_share_one_advantage() -> None:
    segments = build_segments(12, (4, 8))
    assert [(item.start_step, item.end_step) for item in segments] == [(0, 4), (4, 8), (8, 12)]
    assert all(item.advantage_share == pytest.approx(1 / 3) for item in segments)
    single = build_segments(10)
    assert len(single) == 1
    assert single[0].end_step == 10
