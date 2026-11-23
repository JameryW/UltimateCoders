"""Tests for the MEMENTO active-window compactor (V26 #117)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.memento_compactor import (
    MEMENTO_COMPACTOR_ENV,
    MementoCompactor,
    MementoError,
    memento_compactor_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MEMENTO_COMPACTOR_ENV, raising=False)
    assert not memento_compactor_enabled()
    monkeypatch.setenv(MEMENTO_COMPACTOR_ENV, "on")
    assert memento_compactor_enabled()


def test_under_budget_does_not_compact() -> None:
    compactor = MementoCompactor(max_window=1000)
    compactor.record(1, "short observation", 10)
    result = compactor.compact()
    assert not result.compacted
    assert result.note is None
    assert not compactor.notes
    assert compactor.active_tokens == 10


def test_compaction_folds_oldest_entries() -> None:
    compactor = MementoCompactor(max_window=50, compact_fraction=0.5)
    for step in range(1, 4):
        compactor.record(step, f"observation {step}", 20)
    result = compactor.compact()
    assert result.compacted
    assert result.note is not None
    assert result.folded_steps == (1, 2, 3)
    assert compactor.active_tokens <= 50
    assert not compactor.active
    assert len(compactor.notes) == 1


def test_note_keeps_steps_and_reduces_tokens() -> None:
    compactor = MementoCompactor(max_window=50)
    for step in range(1, 4):
        compactor.record(step, f"line {step}", 20)
    note = compactor.compact().note
    assert note is not None
    assert note.steps == (1, 2, 3)
    assert "[1] line 1" in note.content
    assert note.tokens == 15
    assert note.confidence == pytest.approx(1.0)


def test_active_tokens_include_notes() -> None:
    compactor = MementoCompactor(max_window=50)
    for step in range(1, 4):
        compactor.record(step, f"line {step}", 20)
    compactor.compact()
    assert compactor.note_tokens == 15
    assert compactor.active_tokens == 15


def test_traceback_returns_earlier_high_confidence_note() -> None:
    compactor = MementoCompactor(max_window=50, confidence_threshold=0.6)
    compactor.record(1, "steady start", 20, confidence=0.9)
    compactor.record(2, "steady", 20, confidence=0.9)
    compactor.record(3, "filler", 20, confidence=0.95)
    compactor.compact()
    compactor.record(4, "bad again", 20, confidence=0.2)
    compactor.record(5, "filler two", 20, confidence=0.95)
    compactor.compact()
    assert len(compactor.notes) == 2
    traceback = compactor.traceback(5)
    assert traceback is not None
    assert traceback.note.note_id == "note-1"
    assert traceback.target_step == 5
    assert "confidence" in traceback.reason


def test_traceback_returns_none_when_latest_note_is_confident() -> None:
    compactor = MementoCompactor(max_window=64, confidence_threshold=0.6)
    for step in range(1, 4):
        compactor.record(step, f"line {step}", 20, confidence=0.9)
    compactor.compact()
    assert compactor.traceback(4) is None


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(MementoError):
        MementoCompactor(max_window=10)
    with pytest.raises(MementoError):
        MementoCompactor(compact_fraction=0.0)
    with pytest.raises(MementoError):
        MementoCompactor(max_notes=0)
    with pytest.raises(MementoError):
        MementoCompactor(confidence_threshold=1.2)


def test_invalid_entry_is_rejected() -> None:
    compactor = MementoCompactor()
    with pytest.raises(MementoError):
        compactor.record(-1, "x", 1)
    with pytest.raises(MementoError):
        compactor.record(1, "  ", 1)
    with pytest.raises(MementoError):
        compactor.record(1, "x", 0)
    with pytest.raises(MementoError):
        compactor.record(1, "x", 1, confidence=1.5)


def test_render_active_is_bounded() -> None:
    compactor = MementoCompactor()
    compactor.record(1, "a" * 100, 10)
    assert len(compactor.render_active(max_chars=20)) == 20
    assert "[1]" in compactor.render_active(max_chars=1000)


def test_reset_clears_state() -> None:
    compactor = MementoCompactor(max_window=64)
    for step in range(1, 4):
        compactor.record(step, f"line {step}", 20)
    compactor.compact()
    compactor.reset()
    assert not compactor.active
    assert not compactor.notes
    assert compactor.active_tokens == 0
