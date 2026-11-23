"""Tests for the v11 SWE-Shift guard (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.shift_guard import (
    UC_SHIFT_GUARD,
    ShiftGuard,
    ShiftKind,
    ShiftPhase,
    WorkspaceEpoch,
    shift_guard_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_SHIFT_GUARD, raising=False)
    assert shift_guard_enabled() is False
    monkeypatch.setenv(UC_SHIFT_GUARD, "1")
    assert shift_guard_enabled() is True


def test_unchanged_epoch_is_a_noop() -> None:
    guard = ShiftGuard(WorkspaceEpoch("abc", 1, 1))
    assert guard.observe_epoch(WorkspaceEpoch("abc", 1, 1)) is None
    assert guard.phase is ShiftPhase.STABLE
    assert guard.can_write is True


def test_collaborator_commit_runs_three_step_protocol() -> None:
    guard = ShiftGuard(WorkspaceEpoch("abc", 1, 1))
    event = guard.observe_epoch(
        WorkspaceEpoch("def", 1, 1), changed_paths=["src/lib.rs"]
    )
    assert event is not None
    assert event.kind is ShiftKind.COLLABORATOR_COMMIT
    assert guard.phase is ShiftPhase.FROZEN
    assert guard.can_write is False
    plan = guard.plan
    assert plan is not None
    assert plan.read_only_refresh is True
    assert plan.history_reset is True
    assert plan.must_observe_paths == ("src/lib.rs",)

    guard.acknowledge_refresh()
    guard.apply_history_reset()
    assert guard.record_fresh_observation("src/lib.rs") is True
    assert guard.phase is ShiftPhase.READY
    assert guard.can_write is True


def test_requirement_revision_takes_precedence() -> None:
    guard = ShiftGuard(WorkspaceEpoch("abc", 1, 1))
    event = guard.observe_epoch(
        WorkspaceEpoch("def", 2, 2),
        changed_paths=["docs/spec.md"],
        added_symbols=["NewHook"],
    )
    assert event is not None
    assert event.kind is ShiftKind.REQUIREMENT_REVISION
    assert guard.plan is not None
    assert "NewHook" in guard.plan.preserved_symbols


def test_tool_session_rebind_requires_wildcard_observation() -> None:
    guard = ShiftGuard(WorkspaceEpoch("abc", 1, 1))
    guard.observe_epoch(WorkspaceEpoch("abc", 2, 1))
    guard.acknowledge_refresh()
    guard.apply_history_reset()
    assert guard.remaining_observations() == ("*",)
    assert guard.record_fresh_observation("src/lib.rs") is False
    assert guard.record_fresh_observation("*") is True


def test_interface_change_preserves_added_symbols() -> None:
    guard = ShiftGuard(WorkspaceEpoch("abc", 1, 1))
    guard.observe_epoch(
        WorkspaceEpoch("def", 1, 1),
        changed_paths=["src/cache.rs"],
        added_symbols=["guard_lock"],
    )
    with pytest.raises(ValueError, match="preserved collaborator symbol"):
        guard.verify_symbols_preserved(["guard_lock"])
    guard.verify_symbols_preserved(["old_helper"])


def test_stale_evidence_freezes_writes() -> None:
    guard = ShiftGuard(WorkspaceEpoch("abc", 1, 1))
    event = guard.observe_stale_evidence("cached read is stale", ["src/lib.rs"])
    assert event.kind is ShiftKind.STALE_EVIDENCE
    assert guard.phase is ShiftPhase.FROZEN
    assert guard.can_write is False


def test_recovery_steps_are_ordered() -> None:
    guard = ShiftGuard(WorkspaceEpoch("abc", 1, 1))
    guard.observe_epoch(WorkspaceEpoch("def", 1, 1), changed_paths=["src/lib.rs"])
    with pytest.raises(ValueError, match="out of order"):
        guard.apply_history_reset()
    assert guard.record_fresh_observation("src/lib.rs") is False


def test_event_log_is_bounded() -> None:
    guard = ShiftGuard(WorkspaceEpoch("a", 0, 0), max_events=2)
    for index, head in enumerate(("b", "c", "d")):
        guard.observe_epoch(WorkspaceEpoch(head, 0, 0), changed_paths=[f"src/{index}.rs"])
    assert len(guard.events) == 2
    assert guard.events[-1].current.git_head == "d"
