"""Tests for the explicit run-state ledger wiring (v20 #80).

``search/state_filter.py`` owns the deterministic half of the ledger item — a
typed event in, a rule engine out.  These tests cover the caller the harness
gained: the keyed snapshot, the thrash signal, and the wiring into the local
loop.  The filter itself is covered by ``test_v20_state_filter.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ultimate_coders.agent import harness_local_loop as hll
from ultimate_coders.agent.state_ledger import (
    MAX_RENDER_CHARS,
    STATE_LEDGER_ENV,
    THRASH_THRESHOLD,
    StateLedger,
    ThrashWarning,
    state_ledger_enabled,
)
from ultimate_coders.search.state_filter import STATE_FILTER_ENV


def _resp(tool_calls: list[tuple[str, dict]], text: str = "") -> SimpleNamespace:
    calls = [
        SimpleNamespace(
            id=f"c{i}",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for i, (name, args) in enumerate(tool_calls)
    ]
    choice = SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    return SimpleNamespace(choices=[choice], usage=usage)


class TestFlag:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_both_flags_enable_the_ledger(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STATE_LEDGER_ENV, value)
        monkeypatch.setenv(STATE_FILTER_ENV, "1")
        assert state_ledger_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "no", "off", "maybe"])
    def test_falsey_values_keep_it_off(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STATE_LEDGER_ENV, value)
        monkeypatch.setenv(STATE_FILTER_ENV, "1")
        assert state_ledger_enabled() is False

    def test_requires_the_state_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STATE_LEDGER_ENV, "1")
        monkeypatch.delenv(STATE_FILTER_ENV, raising=False)
        assert state_ledger_enabled() is False


class TestLedger:
    def test_write_records_a_keyed_entry(self) -> None:
        ledger = StateLedger()
        assert ledger.record_write(3, "src/app.py") is None
        assert ledger.entries() == {"file:src/app.py": "written"}

    def test_paths_are_normalised(self) -> None:
        ledger = StateLedger()
        ledger.record_write(1, "./src\\app.py")
        assert ledger.entries() == {"file:src/app.py": "written"}

    def test_empty_path_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            StateLedger().record_write(1, "   ")

    def test_threshold_must_be_at_least_two(self) -> None:
        with pytest.raises(ValueError):
            StateLedger(thrash_threshold=1)

    def test_thrash_fires_once_at_the_threshold(self) -> None:
        ledger = StateLedger()
        assert ledger.record_write(1, "a.py") is None
        assert ledger.record_write(2, "a.py") is None
        warning = ledger.record_write(3, "a.py")
        assert isinstance(warning, ThrashWarning)
        assert warning.to_dict() == {"step": 3, "path": "a.py", "writes": 3}
        assert ledger.record_write(4, "a.py") is None
        assert ledger.thrashing() == ("a.py",)

    def test_threshold_is_configurable(self) -> None:
        ledger = StateLedger(thrash_threshold=2)
        assert ledger.record_write(1, "a.py") is None
        assert ledger.record_write(2, "a.py") is not None

    def test_separate_paths_count_separately(self) -> None:
        ledger = StateLedger()
        for step in range(1, THRASH_THRESHOLD):
            assert ledger.record_write(step, "a.py") is None
            assert ledger.record_write(step, "b.py") is None
        assert ledger.thrashing() == ()

    def test_passing_command_clears_the_counts(self) -> None:
        ledger = StateLedger()
        for step in range(1, THRASH_THRESHOLD + 1):
            ledger.record_write(step, "a.py")
        ledger.record_command(9, "pytest -q", 0)
        assert ledger.thrashing() == ()
        assert ledger.record_write(10, "a.py") is None

    def test_failing_command_keeps_the_counts(self) -> None:
        ledger = StateLedger()
        for step in range(1, THRASH_THRESHOLD + 1):
            ledger.record_write(step, "a.py")
        ledger.record_command(9, "pytest -q", 1)
        assert ledger.thrashing() == ("a.py",)
        assert ledger.record_write(10, "a.py") is None

    def test_command_events_land_in_the_state(self) -> None:
        ledger = StateLedger()
        ledger.record_command(4, "  cargo   test  ", 0)
        entries = ledger.entries()
        assert entries["command:last"] == "cargo test"
        assert any(
            key.startswith("command:") and key != "command:last"
            for key in entries
        )

    def test_empty_command_is_ignored(self) -> None:
        ledger = StateLedger()
        ledger.record_command(1, "   ", 2)
        assert ledger.entries() == {}

    def test_render_is_bounded(self) -> None:
        ledger = StateLedger(max_keys=64)
        for index in range(64):
            ledger.record_write(index + 1, f"src/module_{index:02d}_file.py")
        rendered = ledger.render()
        assert len(rendered) <= MAX_RENDER_CHARS
        assert rendered.endswith("...")

    def test_render_lists_thrash(self) -> None:
        ledger = StateLedger()
        for step in range(1, THRASH_THRESHOLD + 1):
            ledger.record_write(step, "a.py")
        assert "thrash: a.py" in ledger.render()

    def test_to_dict_is_json_ready(self) -> None:
        ledger = StateLedger()
        ledger.record_write(1, "a.py")
        ledger.record_command(2, "pytest -q", 0)
        payload = json.loads(json.dumps(ledger.to_dict()))
        assert payload["thrash"] == []
        assert payload["thrash_threshold"] == THRASH_THRESHOLD
        assert payload["stats"]["applied"] >= 2
        assert {"key", "value", "timestamp", "dependencies"} <= set(
            payload["entries"][0]
        )


class TestHarnessWiring:
    async def test_repeat_writes_are_reported_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(STATE_LEDGER_ENV, "1")
        monkeypatch.setenv(STATE_FILTER_ENV, "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "a.py", "content": "1\n"})]),
                _resp([("write_file", {"path": "a.py", "content": "2\n"})]),
                _resp([("write_file", {"path": "a.py", "content": "3\n"})]),
                _resp([("write_file", {"path": "a.py", "content": "4\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda *a, **k: next(responses)):
            assert await hll.run_loop("write a.py", tmp_path, max_turns=8) == 0
        final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert final["success"] is True
        assert final["state_ledger"]["thrash"] == ["a.py"]
        assert final["state_ledger"]["thrash_threshold"] == THRASH_THRESHOLD

    async def test_passing_command_resets_thrash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(STATE_LEDGER_ENV, "1")
        monkeypatch.setenv(STATE_FILTER_ENV, "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "a.py", "content": "1\n"})]),
                _resp([("write_file", {"path": "a.py", "content": "2\n"})]),
                _resp([("run_command", {"command": "exit 0"})]),
                _resp([("write_file", {"path": "a.py", "content": "3\n"})]),
                _resp([("write_file", {"path": "a.py", "content": "4\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda *a, **k: next(responses)):
            assert await hll.run_loop("write a.py", tmp_path, max_turns=10) == 0
        final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert final["state_ledger"]["thrash"] == []
        assert final["state_ledger"]["stats"]["applied"] >= 5

    async def test_disabled_flag_leaves_the_loop_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.delenv(STATE_LEDGER_ENV, raising=False)
        responses = iter(
            [
                _resp([("write_file", {"path": "a.py", "content": "1\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda *a, **k: next(responses)):
            assert await hll.run_loop("write a.py", tmp_path, max_turns=5) == 0
        final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "state_ledger" not in final


class TestWrittenPath:
    def test_reads_the_written_field(self) -> None:
        assert hll._written_path('{"written": "src/a.py"}') == "src/a.py"

    @pytest.mark.parametrize(
        "payload",
        ['{"error": "nope"}', "[]", "not json", '{"written": ""}', '{"written": 3}'],
    )
    def test_rejects_anything_else(self, payload: str) -> None:
        assert hll._written_path(payload) is None
