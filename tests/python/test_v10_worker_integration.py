"""Worker boundary tests for the opt-in V10 PerceptionPass and anti-pattern gates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import FileChange
from ultimate_coders.agent.worker import Worker, _parse_sandbox_line


def test_perception_pass_appends_read_only_spec(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def build(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        description="Add a builder in `pkg/core.py` that returns a value plus one.",
        file_constraints=["pkg/core.py"],
    )

    monkeypatch.delenv("UC_PERCEPTION_PASS", raising=False)
    assert worker._apply_perception_pass(subtask, "base context", str(tmp_path)) == "base context"

    monkeypatch.setenv("UC_PERCEPTION_PASS", "1")
    rendered = worker._apply_perception_pass(subtask, "base context", str(tmp_path))
    assert "base context" in rendered
    assert "## Implementation spec (PerceptionPass)" in rendered
    assert "`pkg/core.py`" in rendered


def test_perception_pass_failure_keeps_original_context(tmp_path: Path, monkeypatch) -> None:
    from ultimate_coders.agent import planner

    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        description="Touch `missing.py`.",
        file_constraints=["missing.py"],
    )
    monkeypatch.setenv("UC_PERCEPTION_PASS", "1")
    monkeypatch.setattr(
        planner.PerceptionPass,
        "compile",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert worker._apply_perception_pass(subtask, "unchanged", str(tmp_path)) == "unchanged"


def test_drift_hints_use_declared_file_constraints() -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(file_constraints=["src/a.py", "src/b.py"])
    assert worker._drift_hints(subtask) == {
        "_uc_drift_boundary": "src/a.py,src/b.py",
        "_uc_drift_required_files": "src/a.py,src/b.py",
    }

    subtask = SimpleNamespace(file_constraints=[])
    assert worker._drift_hints(subtask) == {}


def test_drift_advisor_line_becomes_task_progress() -> None:
    parsed = _parse_sandbox_line(
        json.dumps(
            {
                "event": "drift_advisor",
                "signals": ["edit_failures"],
                "prompt": "Stop editing and re-read.",
            }
        )
    )
    assert parsed is not None
    event_type, data = parsed
    assert event_type == "subtask_progress"
    assert data["phase"] == "drift advisor"
    assert data["drift_signals"] == ["edit_failures"]
    assert data["drift_prompt"] == "Stop editing and re-read."


async def test_anti_pattern_gate_rejects_deleted_assertion(tmp_path: Path, monkeypatch) -> None:
    test_file = tmp_path / "tests" / "test_calc.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "def test_add():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    test_file.write_text("def test_add():\n    pass\n", encoding="utf-8")
    output = AgentOutput(
        file_changes=[FileChange(file_path="tests/test_calc.py")],
        commands_run=["pytest -q"],
    )
    worker = object.__new__(Worker)
    monkeypatch.setenv("UC_ANTI_PATTERN_GATE", "1")

    report = await worker._verify_output_anti_patterns(str(tmp_path), output)
    assert report is not None
    assert not report.passed
    assert any(item.kind == "test_file_tampering" for item in report.errors)
