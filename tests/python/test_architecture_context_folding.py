"""Tests for ACP interval folding and verified-read protection."""

from __future__ import annotations

import json

import pytest
from ultimate_coders.architecture.context_folding import (
    ACPFolder,
    FoldCheckpoint,
    ReadLedger,
    _tools_in,
    estimate_messages_tokens,
    fold_messages,
    folding_enabled,
    folding_threshold,
)


def _message(role: str, content: str = "", **extra: object) -> dict[str, object]:
    message: dict[str, object] = {"role": role, "content": content}
    message.update(extra)
    return message


def _tool_message(tool_call_id: str, payload: object) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload),
    }


def _assistant_call(call_id: str, name: str) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    }


def test_estimate_messages_tokens_is_positive_and_monotonic() -> None:
    assert estimate_messages_tokens([]) == 0
    small = [_message("user", "x" * 40)]
    large = [_message("user", "x" * 400)]
    assert estimate_messages_tokens(small) > 0
    assert estimate_messages_tokens(large) > estimate_messages_tokens(small)


def test_folder_folds_middle_and_preserves_head_and_tail() -> None:
    system = _message("system", "you are a coder")
    task = _message("user", "fix the bug")
    middle = [
        _message("assistant", "thinking " + "x" * 200),
        _assistant_call("1", "read_file"),
        _tool_message("1", {"content": "alpha", "next_offset": 5}),
        _message("assistant", "thinking " + "y" * 200),
        _assistant_call("2", "write_file"),
        _tool_message("2", {"written": "a.py"}),
    ]
    tail = [_message("assistant", "done"), _message("user", "continue")]
    folder = ACPFolder(budget_tokens=1000, threshold=0.7, keep_tail=2)
    result = folder.fold([system, task, *middle, *tail], turn=3)

    assert result.folded is True
    assert result.messages[0] == system
    assert result.messages[1] == task
    assert result.messages[-2:] == tail
    assert result.folded_messages == len(middle)
    assert result.saved_tokens > 0
    assert "ACP fold" in result.messages[2]["content"]
    assert "a.py" in result.messages[2]["content"]
    assert folder.checkpoints[-1].turn == 3


def test_folder_does_not_fold_without_a_middle_interval() -> None:
    folder = ACPFolder(budget_tokens=100, threshold=0.9, keep_tail=10)
    messages = [_message("system", "s"), _message("user", "u")]
    result = folder.fold(messages)
    assert result.folded is False
    assert result.messages == messages


def test_fold_boundary_does_not_split_assistant_tool_calls_from_results() -> None:
    folder = ACPFolder(budget_tokens=1000, threshold=0.5, keep_tail=2)
    messages = [
        _message("system", "s"),
        _message("user", "task"),
        _message("assistant", "thinking " + "x" * 120),
        _assistant_call("1", "read_file"),
        _tool_message("1", {"content": "a"}),
        _message("assistant", "done"),
    ]
    result = folder.fold(messages)
    assert result.folded is True
    # The kept tail starts at the assistant tool-call, not at its orphan tool
    # result, and the fold summary is the only message before it.
    assert result.messages[2]["role"] == "system"
    assert "ACP fold" in result.messages[2]["content"]
    assert result.messages[3]["role"] == "assistant"
    assert _tools_in(result.messages[3]) == ["read_file"]
    assert result.messages[4]["role"] == "tool"
    assert result.messages[5] == _message("assistant", "done")


def test_read_ledger_blocks_only_unchanged_duplicate_ranges() -> None:
    ledger = ReadLedger()
    first = ledger.record("a.py", 0, "alpha", turn=1)
    assert first.path == "a.py"
    assert ledger.duplicate("a.py", 0, "alpha") is True
    assert ledger.blocked == 1
    assert ledger.duplicate("a.py", 0, "alpha changed") is False
    assert ledger.duplicate("a.py", 5, "alpha") is False
    assert ledger.duplicate("b.py", 0, "alpha") is False
    assert len(ledger.hashes()) == 1


def test_checkpoint_round_trips() -> None:
    checkpoint = FoldCheckpoint(
        checkpoint_id="fold-1-abc",
        turn=2,
        folded_messages=4,
        kept_messages=3,
        saved_tokens=120,
        read_hashes=("a.py@0:abc",),
        summary="summary",
    )
    assert FoldCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint


def test_maybe_fold_respects_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_ACP_FOLDING", raising=False)
    messages = [
        _message("system", "s"),
        _message("user", "task"),
        _message("assistant", "x" * 5000),
        _assistant_call("1", "read_file"),
        _tool_message("1", {"content": "a"}),
        _message("assistant", "done"),
    ]
    result = fold_messages(messages)
    assert result.folded is False

    monkeypatch.setenv("UC_ACP_FOLDING", "1")
    folder = ACPFolder(budget_tokens=100, threshold=0.5, keep_tail=2)
    assert folder.should_fold(messages) is True
    assert folder.maybe_fold(messages).folded is True


def test_threshold_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UC_ACP_FOLD_THRESHOLD", "0.1")
    assert folding_threshold() == 0.5
    monkeypatch.setenv("UC_ACP_FOLD_THRESHOLD", "0.99")
    assert folding_threshold() == 0.95
    monkeypatch.setenv("UC_ACP_FOLD_THRESHOLD", "not-a-number")
    assert folding_threshold() == 0.70


def test_folding_enabled_reads_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UC_ACP_FOLDING", "yes")
    assert folding_enabled() is True
    monkeypatch.setenv("UC_ACP_FOLDING", "0")
    assert folding_enabled() is False
