"""Tests for the PROBE failure brief (#70) wired into the worker step bridge."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from ultimate_coders.agent.probe_brief import (
    MAX_ACTIONS,
    MAX_BRIEF_CHARS,
    FailureInputs,
    build_telemetry,
    failure_brief,
    is_test_command,
    probe_brief_enabled,
)
from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import FileChange
from ultimate_coders.agent.worker import Worker


def _flags(monkeypatch: pytest.MonkeyPatch, *, brief: str = "1", recovery: str = "1") -> None:
    monkeypatch.setenv("UC_PROBE_BRIEF", brief)
    monkeypatch.setenv("UC_PROBE_RECOVERY", recovery)


class TestFlag:
    def test_disabled_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UC_PROBE_BRIEF", raising=False)
        monkeypatch.delenv("UC_PROBE_RECOVERY", raising=False)
        assert probe_brief_enabled() is False

    def test_requires_the_probe_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch, brief="1", recovery="")
        assert probe_brief_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        _flags(monkeypatch, brief=value)
        assert probe_brief_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
    def test_falsey_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        _flags(monkeypatch, brief=value)
        assert probe_brief_enabled() is False


class TestIsTestCommand:
    @pytest.mark.parametrize(
        "command",
        [
            "pytest -q",
            "python -m pytest tests/python",
            "python -m unittest discover",
            "cargo test --lib",
            "cargo nextest run",
            "npm test",
            "npm run test:unit",
            "pnpm test",
            "yarn test",
            "bun test src",
            "go test ./...",
            "tox -e py312",
            "nox",
            "jest --ci",
            "vitest run",
            "bundle exec rspec",
            "phpunit",
        ],
    )
    def test_recognised(self, command: str) -> None:
        assert is_test_command(command) is True

    @pytest.mark.parametrize(
        "command",
        ["", "   ", "git status", "ls -la", "python -m compileall .", "cargo build"],
    )
    def test_not_recognised(self, command: str) -> None:
        assert is_test_command(command) is False


@dataclass
class _Change:
    file_path: str = ""


@dataclass
class _Stand:
    summary: str = ""
    stderr_tail: str = ""
    test_output: str = ""
    tool_calls: list[str] | None = None
    commands_run: list[str] | None = None
    file_changes: list[_Change] | None = None


class TestFailureInputs:
    def test_normalises_text_and_sequences(self) -> None:
        inputs = FailureInputs(
            summary="  boom  ",
            tool_calls=[" write_file ", "", "  "],
            commands_run="pytest -q",  # str accepted as a single item
            file_paths=["src/app.py", " src/cache.py "],
        )
        assert inputs.summary == "boom"
        assert inputs.tool_calls == ("write_file",)
        assert inputs.commands_run == ("pytest -q",)
        assert inputs.file_paths == ("src/app.py", "src/cache.py")

    def test_empty_detection(self) -> None:
        assert FailureInputs().empty is True
        assert FailureInputs(summary="  ", stderr_tail=" ").empty is True
        assert FailureInputs(file_paths=("src/app.py",)).empty is False
        assert FailureInputs(tool_calls=("ls",)).empty is False

    def test_from_output_reads_duck_typed_fields(self) -> None:
        stand = _Stand(
            summary="  step failed ",
            stderr_tail=" Traceback ... ",
            tool_calls=["write_file"],
            commands_run=["pytest -q tests"],
            file_changes=[_Change("src/app.py"), _Change("  ")],
        )
        inputs = FailureInputs.from_output(stand)
        assert inputs.summary == "step failed"
        assert inputs.stderr_tail == "Traceback ..."
        assert inputs.tool_calls == ("write_file",)
        assert inputs.commands_run == ("pytest -q tests",)
        assert inputs.file_paths == ("src/app.py",)

    def test_from_output_tolerates_objects_without_fields(self) -> None:
        assert FailureInputs.from_output(object()).empty is True
        assert FailureInputs.from_output(None).empty is True


class TestBuildTelemetry:
    def test_prefers_test_output_over_stderr(self) -> None:
        telemetry = build_telemetry(
            FailureInputs(
                stderr_tail="Traceback (most recent call last):",
                test_output="AssertionError: 1 != 2",
            )
        )
        assert telemetry.stderr == "AssertionError: 1 != 2"

    def test_falls_back_to_stderr_tail(self) -> None:
        telemetry = build_telemetry(FailureInputs(stderr_tail="ValueError: bad"))
        assert telemetry.stderr == "ValueError: bad"

    def test_tool_defaults_to_sandbox(self) -> None:
        assert build_telemetry(FailureInputs(stderr_tail="x")).tool == "sandbox"

    def test_tool_and_command_come_from_the_tail(self) -> None:
        telemetry = build_telemetry(
            FailureInputs(
                tool_calls=("read_file", "run_command"),
                commands_run=("ls", "pytest -q tests/python"),
            )
        )
        assert telemetry.tool == "run_command"
        assert telemetry.command == "pytest -q tests/python"

    def test_only_test_commands_become_test_names(self) -> None:
        telemetry = build_telemetry(
            FailureInputs(commands_run=("ls", "pytest -q", "cargo test", "cargo test --lib"))
        )
        assert telemetry.test_names == ("pytest -q", "cargo test")


class TestFailureBrief:
    def test_none_when_there_is_no_signal(self) -> None:
        assert failure_brief(None) is None
        assert failure_brief(object()) is None
        assert failure_brief(None, inputs=FailureInputs()) is None

    def test_grounded_diagnosis_with_bounded_actions(self) -> None:
        brief = failure_brief(
            None,
            inputs=FailureInputs(
                stderr_tail="AssertionError: cache miss",
                file_paths=("src/app.py",),
                commands_run=("pytest -q tests",),
            ),
        )
        assert brief is not None
        assert brief["grounded"] is True
        assert brief["confidence"] == pytest.approx(0.8)
        assert brief["main_cause"] == "AssertionError: cache miss"
        assert brief["failure_anchor"] == "src/app.py"
        assert brief["signal_gaps"] == ["context", "api_usage"]
        assert 0 < len(brief["actions"]) <= MAX_ACTIONS
        assert brief["next_attempt"].startswith("Next attempt: AssertionError: cache miss")

    def test_ungrounded_signal_produces_no_guidance(self) -> None:
        brief = failure_brief(None, inputs=FailureInputs(tool_calls=("ls",)))
        assert brief is not None
        assert brief["grounded"] is False
        assert brief["confidence"] == pytest.approx(0.2)
        assert brief["failure_anchor"] == "ls"
        assert brief["actions"] == []
        assert brief["next_attempt"] == ""

    def test_max_actions_is_clamped(self) -> None:
        inputs = FailureInputs(
            stderr_tail="boom", file_paths=("src/a.py",), commands_run=("pytest",)
        )
        assert len(failure_brief(None, inputs=inputs, max_actions=1)["actions"]) == 1
        assert len(failure_brief(None, inputs=inputs, max_actions=0)["actions"]) == 1
        assert len(failure_brief(None, inputs=inputs, max_actions=99)["actions"]) <= 6

    def test_text_fields_are_truncated(self) -> None:
        brief = failure_brief(None, inputs=FailureInputs(stderr_tail="X" * 1000))
        assert brief is not None
        assert len(brief["main_cause"]) <= 120

    def test_brief_is_bounded_and_json_serialisable(self) -> None:
        brief = failure_brief(
            None,
            inputs=FailureInputs(
                stderr_tail="Y" * 5000,
                file_paths=("src/" + "deep/" * 200 + "app.py",),
                commands_run=("pytest -q",),
                tool_calls=("write_file",),
            ),
        )
        assert brief is not None
        encoded = json.dumps(brief)
        assert len(encoded) <= MAX_BRIEF_CHARS


def _failed_output(**overrides: object) -> AgentOutput:
    values: dict[str, object] = {
        "summary": "step failed",
        "success": False,
        "stderr_tail": "AssertionError: cache miss",
        "tool_calls": ["write_file"],
        "commands_run": ["pytest -q tests/python"],
        "file_changes": [FileChange(file_path="src/app.py", diff="+x")],
    }
    values.update(overrides)
    return AgentOutput(**values)  # type: ignore[arg-type]


class TestWorkerBridge:
    def test_failed_step_carries_the_brief(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        payload = json.loads(Worker._output_to_json(_failed_output()))
        brief = payload["failure_diagnosis"]
        assert brief["main_cause"] == "AssertionError: cache miss"
        assert brief["failure_anchor"] == "src/app.py"
        assert brief["grounded"] is True

    def test_successful_step_has_no_brief(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        payload = json.loads(Worker._output_to_json(_failed_output(success=True)))
        assert "failure_diagnosis" not in payload

    def test_disabled_flag_keeps_the_legacy_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch, brief="0")
        payload = json.loads(Worker._output_to_json(_failed_output()))
        assert "failure_diagnosis" not in payload
        assert payload["stderr_tail"] == "AssertionError: cache miss"

    def test_probe_engine_off_leaves_the_payload_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flags(monkeypatch, recovery="0")
        payload = json.loads(Worker._output_to_json(_failed_output()))
        assert "failure_diagnosis" not in payload

    def test_signal_free_failure_adds_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        payload = json.loads(
            Worker._output_to_json(
                _failed_output(stderr_tail="", commands_run=[], file_changes=[], tool_calls=[])
            )
        )
        assert "failure_diagnosis" not in payload

    def test_none_output_is_still_an_empty_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        assert Worker._output_to_json(None) == "{}"
