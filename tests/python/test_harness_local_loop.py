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
    _safe_resolve,
    _tool_list_dir,
    _tool_read_file,
    _tool_write_file,
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


class TestAdapterParse:
    def _parse(self, stdout: str, **kw: object) -> object:
        return LocalLoopHarnessAdapter().parse_output(
            ExecResult(stdout=stdout, exit_code=kw.get("exit_code", 0))
        )

    def test_final_envelope_success(self) -> None:
        stdout = "\n".join([
            json.dumps({"event": "tool_call", "tool": "read_file"}),
            json.dumps({"event": "tool_call", "tool": "write_file"}),
            json.dumps({"event": "final", "success": True, "summary": "all done"}),
        ])
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
        stdout = "\n".join([
            json.dumps({"event": "final", "success": True, "summary": "first"}),
            json.dumps({"event": "final", "success": False, "summary": "second"}),
        ])
        envelope = _final_envelope(stdout)
        assert envelope is not None and envelope["summary"] == "second"


class TestRunnerTools:
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        responses = iter([
            _resp([("list_dir", {"path": "."})], text="looking"),
            _resp([("write_file", {"path": "out.txt", "content": "hi"})]),
            _resp([("done", {"summary": "wrote out.txt"})], text="finishing"),
        ])
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 10))
        assert code == 0
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        tools = [ln["tool"] for ln in lines if ln.get("event") == "tool_call"]
        assert tools == ["list_dir", "write_file", "done"]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is True
        assert final["files_written"] == ["out.txt"]
        assert final["turns"] == 3

    def test_max_turns_exhaustion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
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

    def test_runner_main_deletes_prompt_file(self, tmp_path: Path) -> None:
        prompt = tmp_path / "p.md"
        prompt.write_text("hello", encoding="utf-8")
        code = hll.main(["--prompt-file", str(prompt), "--cwd", str(tmp_path),
                         "--max-turns", "1"])
        assert not prompt.exists()
        # No API key configured path may vary; main itself must return an int.
        assert isinstance(code, int)
