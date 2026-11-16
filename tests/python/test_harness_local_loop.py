"""Tests for the lightweight local-LLM harness plugin.

Covers adapter request building, parse_output (envelope/tool-call/timeout/
fallback paths), the runner tool implementations (path safety, roundtrips),
and the full agent loop with a stubbed completion function (done path and
max-turns exhaustion).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ultimate_coders.agent import harness_local_loop as hll
from ultimate_coders.agent.harness_local_loop import (
    LocalLoopHarnessAdapter,
    _final_envelope,
    _normalise_rel_path,
    _safe_resolve,
    _tool_list_dir,
    _tool_read_file,
    _tool_write_file,
    _within_boundary,
)
from ultimate_coders.agent.registry import ensure_builtin_plugins, registry
from ultimate_coders.agent.sandbox import ExecResult, SandboxConfig


class TestRegistration:
    def test_builtin_registered_with_alias(self) -> None:
        ensure_builtin_plugins()
        assert registry.get_spec("local-harness") is not None
        assert registry.get_spec("local-llm") is registry.get_spec("local-harness")
        spec = registry.get_spec("local-harness")
        assert spec is not None and spec.cli_probe is None  # API-backed


class TestAdapterRequest:
    def test_build_request_shape(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(agent="local-harness", project_path=str(tmp_path))
        request = LocalLoopHarnessAdapter().build_request("do it", str(tmp_path), cfg)

        assert request["command"] == sys.executable
        idx = request["args"].index("-m")
        assert request["args"][idx + 1] == "ultimate_coders.agent.harness_local_loop"
        assert "--cwd" in request["args"]
        prompt_file = request["args"][request["args"].index("--prompt-file") + 1]
        with open(prompt_file, encoding="utf-8") as f:
            assert f.read() == "do it"
        assert prompt_file in request["_temp_files"]
        os.unlink(prompt_file)

    def test_subtask_max_turns_override(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(agent="local-harness", project_path=str(tmp_path))
        request = LocalLoopHarnessAdapter().build_request(
            "p", str(tmp_path), cfg, subtask_config={"max_turns": 7}
        )
        args = request["args"]
        assert args[args.index("--max-turns") + 1] == "7"
        os.unlink(request["_temp_files"][0])

    def test_drift_task_hints_become_child_env(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(agent="local-harness", project_path=str(tmp_path))
        request = LocalLoopHarnessAdapter().build_request(
            "p",
            str(tmp_path),
            cfg,
            subtask_config={
                "_uc_drift_boundary": "src/a.py,src/b.py",
                "_uc_drift_required_files": "src/a.py",
            },
        )
        assert request["env_vars"]["UC_DRIFT_BOUNDARY"] == "src/a.py,src/b.py"
        assert request["env_vars"]["UC_DRIFT_REQUIRED_FILES"] == "src/a.py"
        os.unlink(request["_temp_files"][0])


class TestAdapterParse:
    def _parse(self, stdout: str, **kw: object) -> object:
        return LocalLoopHarnessAdapter().parse_output(
            ExecResult(stdout=stdout, exit_code=kw.get("exit_code", 0))
        )

    def test_final_envelope_success(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"event": "tool_call", "tool": "read_file"}),
                json.dumps({"event": "tool_call", "tool": "write_file"}),
                json.dumps({"event": "final", "success": True, "summary": "all done"}),
            ]
        )
        out = self._parse(stdout)
        assert out.success is True
        assert out.summary == "all done"
        assert out.tool_calls == ["read_file", "write_file"]

    def test_final_envelope_failure(self) -> None:
        stdout = json.dumps({"event": "final", "success": False, "summary": "boom"})
        out = self._parse(stdout, exit_code=1)
        assert out.success is False
        assert out.summary == "boom"

    def test_no_envelope_falls_back_to_assistant_text(self) -> None:
        stdout = json.dumps({"event": "assistant", "text": "partial"})
        out = self._parse(stdout, exit_code=1)
        assert out.summary == "partial"
        assert out.success is False

    def test_timeout(self) -> None:
        out = LocalLoopHarnessAdapter().parse_output(
            ExecResult(stdout="", exit_code=-1, timed_out=True)
        )
        assert out.success is False
        assert "timed out" in out.summary.lower()

    def test_final_envelope_takes_last(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"event": "final", "success": True, "summary": "first"}),
                json.dumps({"event": "final", "success": False, "summary": "second"}),
            ]
        )
        envelope = _final_envelope(stdout)
        assert envelope is not None and envelope["summary"] == "second"

    def test_usage_events_are_summed(self) -> None:
        stdout = "\n".join(
            [
                json.dumps(
                    {"event": "usage", "turn": 1, "prompt_tokens": 11, "completion_tokens": 3}
                ),
                json.dumps({"event": "tool_call", "tool": "read_file"}),
                json.dumps(
                    {"event": "usage", "turn": 2, "prompt_tokens": 7, "completion_tokens": 2}
                ),
                json.dumps({"event": "final", "success": True, "summary": "done"}),
            ]
        )
        out = self._parse(stdout)
        assert out.token_usage is not None
        assert out.token_usage.input_tokens == 18
        assert out.token_usage.output_tokens == 5
        assert out.token_usage.source == "local-harness"

    def test_missing_usage_is_not_reported_as_zero(self) -> None:
        out = self._parse(json.dumps({"event": "final", "success": True, "summary": "done"}))
        assert out.token_usage is None


class TestRunnerTools:
    def test_boundary_paths_resolve_parent_segments(self) -> None:
        assert _normalise_rel_path("./src\\a.py") == "src/a.py"
        assert _within_boundary("src/sub/a.py", ("src",)) is True
        assert _within_boundary("src/../other/a.py", ("src",)) is False
        assert _within_boundary("../outside.py", ("src",)) is False
        assert _within_boundary("anything.py", ()) is True

    def test_safe_resolve_rejects_escape(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes"):
            _safe_resolve(tmp_path, "../x")

    def test_safe_resolve_rejects_empty(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            _safe_resolve(tmp_path, "")

    def test_write_read_list_roundtrip(self, tmp_path: Path) -> None:
        from ultimate_coders.agent.harness_local_loop import _RunStats

        stats = _RunStats()
        payload = {"path": "src/app.py", "content": "print(1)\n"}
        assert json.loads(_tool_write_file(tmp_path, payload, stats))["written"] == "src/app.py"
        read = json.loads(_tool_read_file(tmp_path, {"path": "src/app.py"}))
        assert read["content"] == "print(1)\n"
        listing = json.loads(_tool_list_dir(tmp_path, {"path": "."}))
        assert "src/" in listing["entries"]

    def test_read_missing_file(self, tmp_path: Path) -> None:
        assert "error" in json.loads(_tool_read_file(tmp_path, {"path": "nope"}))

    def test_read_large_file_in_utf8_safe_pages(self, tmp_path: Path) -> None:
        (tmp_path / "readme.md").write_bytes(("# Heading\n" + "界" * 4000).encode("utf-8"))
        first = json.loads(_tool_read_file(tmp_path, {"path": "readme.md"}))
        assert first["content"].startswith("# Heading\n")
        assert first["truncated"] is True
        assert len(first["content"].encode()) <= 8192
        second = json.loads(
            _tool_read_file(
                tmp_path,
                {"path": "readme.md", "offset": first["next_offset"]},
            )
        )
        assert second["truncated"] is False
        expected = (tmp_path / "readme.md").read_text(encoding="utf-8")
        assert first["content"] + second["content"] == expected

    def test_read_ledger_blocks_an_unchanged_repeat_but_allows_changed_content(
        self, tmp_path: Path
    ) -> None:
        from ultimate_coders.architecture.context_folding import ReadLedger

        (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
        ledger = ReadLedger()
        first = json.loads(_tool_read_file(tmp_path, {"path": "a.txt"}, ledger=ledger, turn=1))
        assert first["content"] == "alpha"

        duplicate = json.loads(
            _tool_read_file(tmp_path, {"path": "a.txt"}, ledger=ledger, turn=2)
        )
        assert duplicate["error"] == "duplicate read blocked"

        (tmp_path / "a.txt").write_text("beta", encoding="utf-8")
        changed = json.loads(
            _tool_read_file(tmp_path, {"path": "a.txt"}, ledger=ledger, turn=3)
        )
        assert changed["content"] == "beta"


def _resp(tool_calls: list[tuple[str, dict]], text: str = "") -> SimpleNamespace:
    calls = [
        SimpleNamespace(
            id=f"c{i}",
            function=SimpleNamespace(name=n, arguments=json.dumps(a)),
        )
        for i, (n, a) in enumerate(tool_calls)
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


class TestRunnerLoop:
    def test_full_loop_until_done(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        responses = iter(
            [
                _resp([("list_dir", {"path": "."})], text="looking"),
                _resp([("write_file", {"path": "out.txt", "content": "hi"})]),
                _resp([("done", {"summary": "wrote out.txt"})], text="finishing"),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 10))
        assert code == 0
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        tools = [ln["tool"] for ln in lines if ln.get("event") == "tool_call"]
        assert tools == ["list_dir", "write_file", "done"]
        usage = [ln for ln in lines if ln.get("event") == "usage"]
        assert [row["turn"] for row in usage] == [1, 2, 3]
        assert sum(row["prompt_tokens"] for row in usage) == 3
        assert sum(row["completion_tokens"] for row in usage) == 3
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is True
        assert final["files_written"] == ["out.txt"]
        assert final["turns"] == 3

    def test_max_turns_exhaustion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        endless = _resp([("list_dir", {"path": "."})])
        with patch.object(hll, "_completion", side_effect=lambda m: endless):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 2))
        assert code == 1
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is False
        assert "max turns" in final["summary"]

    def test_loop_exception_emits_failure_envelope(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        def boom(messages):
            raise RuntimeError("api down")

        with patch.object(hll, "_completion", side_effect=boom):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 5))
        assert code == 1
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is False
        assert "api down" in final["summary"]

    def test_acp_folding_emits_event_and_blocks_duplicate_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv("UC_ACP_FOLDING", "1")
        monkeypatch.setenv("UC_ACP_CONTEXT_TOKENS", "512")
        monkeypatch.setenv("UC_ACP_FOLD_THRESHOLD", "0.5")
        monkeypatch.setenv("UC_ACP_KEEP_TAIL", "2")
        (tmp_path / "large.txt").write_text("x" * 4000, encoding="utf-8")
        responses = iter(
            [
                _resp([("read_file", {"path": "large.txt"})]),
                _resp([("read_file", {"path": "large.txt"})]),
                _resp([("done", {"summary": "read it"})]),
            ]
        )
        seen: list[list[dict]] = []

        def completion(messages):
            seen.append([dict(message) for message in messages])
            return next(responses)

        with patch.object(hll, "_completion", side_effect=completion):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 10))
        assert code == 0
        # The second read was already answered by the first one, so the model
        # sees an error instead of a duplicate copy of the file.
        duplicated = [
            message
            for call in seen[2:]
            for message in call
            if message.get("role") == "tool" and "duplicate read blocked" in str(message)
        ]
        assert duplicated
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        folds = [ln for ln in lines if ln.get("event") == "context_fold"]
        assert folds
        assert folds[0]["saved_tokens"] > 0
        assert folds[0]["checkpoint_id"]

    def test_drift_monitor_advises_after_repeated_edit_failures(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("UC_DRIFT_MONITOR", "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "../bad-1.txt", "content": "x"})]),
                _resp([("write_file", {"path": "../bad-2.txt", "content": "x"})]),
                _resp([("write_file", {"path": "../bad-3.txt", "content": "x"})]),
                _resp([("done", {"summary": "stopped"})]),
            ]
        )
        seen: list[list[dict]] = []

        def completion(messages):
            seen.append([dict(message) for message in messages])
            return next(responses)

        with patch.object(hll, "_completion", side_effect=completion):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 6))
        assert code == 0
        assert any(
            message.get("role") == "system"
            and str(message.get("content", "")).startswith("Advisor:")
            for message in seen[3]
        )
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        advisor = [ln for ln in lines if ln.get("event") == "drift_advisor"]
        assert advisor
        assert "edit_failures" in advisor[0]["signals"]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert "edit_failures" in final["drift_signals"]

    def test_drift_monitor_reports_outside_boundary_edits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("UC_DRIFT_MONITOR", "1")
        monkeypatch.setenv("UC_DRIFT_BOUNDARY", "allowed/")
        responses = iter(
            [
                _resp([("write_file", {"path": "other/a.txt", "content": "a"})]),
                _resp([("write_file", {"path": "other/b.txt", "content": "b"})]),
                _resp([("done", {"summary": "done"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 6))
        assert code == 0
        assert (tmp_path / "other" / "a.txt").read_text(encoding="utf-8") == "a"
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        advisor = [ln for ln in lines if ln.get("event") == "drift_advisor"]
        assert advisor
        assert "outside_boundary" in advisor[0]["signals"]

    def test_drift_monitor_injects_budget_advisor_before_last_turn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("UC_DRIFT_MONITOR", "1")
        monkeypatch.setenv("UC_DRIFT_REQUIRED_FILES", "required.py")
        seen: list[list[dict]] = []

        def completion(messages):
            seen.append([dict(message) for message in messages])
            return _resp([("done", {"summary": "no coverage"})])

        with patch.object(hll, "_completion", side_effect=completion):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 1))
        assert code == 0
        assert any(
            message.get("role") == "system"
            and str(message.get("content", "")).startswith("Advisor:")
            for message in seen[0]
        )
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        advisor = [ln for ln in lines if ln.get("event") == "drift_advisor"]
        assert advisor
        assert "budget_zero_coverage" in advisor[0]["signals"]

    def test_ecloop_gate_blocks_unread_edit_then_allows_after_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("UC_ECLOOP_GATE", "1")
        (tmp_path / "notes.txt").write_text("context", encoding="utf-8")
        responses = iter(
            [
                _resp([("write_file", {"path": "out.txt", "content": "value"})]),
                _resp([("read_file", {"path": "notes.txt"})]),
                _resp([("write_file", {"path": "out.txt", "content": "value"})]),
                _resp([("done", {"summary": "done"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 6))
        assert code == 0
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "value"
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        blocked = [ln for ln in lines if ln.get("event") == "ecloop_blocked"]
        assert blocked
        assert blocked[0]["action"] == "edit"
        assert "Missing:" in blocked[0]["prompt"]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["ecloop_blocks"] == {"out.txt": 1}

    def test_runner_main_deletes_prompt_file(self, tmp_path: Path) -> None:
        prompt = tmp_path / "p.md"
        prompt.write_text("hello", encoding="utf-8")
        code = hll.main(["--prompt-file", str(prompt), "--cwd", str(tmp_path), "--max-turns", "1"])
        assert not prompt.exists()
        # No API key configured path may vary; main itself must return an int.
        assert isinstance(code, int)
