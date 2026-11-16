"""Tests for the zero-edit v10 PerceptionPass planner."""

from __future__ import annotations

from pathlib import Path

import pytest
from ultimate_coders.agent.planner import (
    MAPPING_CHECKS,
    FileBoundary,
    ImplementationSpec,
    PerceptionPass,
    compile_spec,
    perception_pass_enabled,
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _package(root: Path) -> None:
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/core.py",
        "from .helpers import helper_value\n\n\ndef build(value):\n"
        "    return helper_value(value) + 1\n",
    )
    _write(
        root,
        "pkg/helpers.py",
        "def helper_value(value):\n    return value * 2\n",
    )


def test_compile_returns_grounded_spec_shape(tmp_path: Path) -> None:
    _package(tmp_path)

    spec = PerceptionPass().compile(
        "task-42",
        "Implement `pkg/core.py`",
        repo_root=str(tmp_path),
        file_constraints=("pkg/core.py",),
    )

    assert isinstance(spec, ImplementationSpec)
    assert spec.task_id == "task-42"
    assert spec.request == "Implement `pkg/core.py`"
    assert [boundary.path for boundary in spec.target_files] == ["pkg/core.py"]
    assert isinstance(spec.target_files[0], FileBoundary)
    assert spec.target_files[0].role == "module"
    assert "pkg/helpers.py" in spec.dependency_closure
    assert "build" in spec.public_api
    assert 0.0 <= spec.confidence <= 1.0
    payload = spec.to_dict()
    assert payload["task_id"] == "task-42"
    assert payload["target_files"][0]["path"] == "pkg/core.py"
    assert len(payload["checks"]) == 8


def test_emits_exactly_eight_named_checks(tmp_path: Path) -> None:
    _package(tmp_path)

    spec = compile_spec(
        "task-1", "Add a builder", repo_root=str(tmp_path), file_constraints=["pkg/core.py"]
    )

    assert tuple(check["name"] for check in spec.checks) == MAPPING_CHECKS
    assert len(MAPPING_CHECKS) == 8
    assert {check["status"] for check in spec.checks} <= {"pass", "warn", "missing"}
    assert all(check["detail"] for check in spec.checks)


def test_two_hop_dependency_closure(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "from .b import beta\n\ndef alpha():\n    return beta()\n")
    _write(tmp_path, "pkg/b.py", "from .c import gamma\n\ndef beta():\n    return gamma()\n")
    _write(tmp_path, "pkg/c.py", "from .d import delta\n\ndef gamma():\n    return delta()\n")
    _write(tmp_path, "pkg/d.py", "def delta():\n    return 4\n")

    spec = PerceptionPass().compile(
        "task-hop", "Update alpha", repo_root=str(tmp_path), file_constraints=("pkg/a.py",)
    )

    assert "pkg/b.py" in spec.dependency_closure
    assert "pkg/c.py" in spec.dependency_closure
    assert "pkg/d.py" not in spec.dependency_closure
    assert any(
        check["name"] == "dependency_closure_mapped" and check["status"] == "pass"
        for check in spec.checks
    )


def test_missing_target_is_handled(tmp_path: Path) -> None:
    spec = PerceptionPass().compile(
        "task-missing",
        "Create the missing module",
        repo_root=str(tmp_path),
        file_constraints=("pkg/does_not_exist.py",),
    )

    statuses = {check["name"]: check for check in spec.checks}
    assert statuses["targets_exist"]["status"] == "missing"
    assert "missing" in statuses["targets_exist"]["detail"]
    assert spec.dependency_closure == []
    assert any("missing targets" in note for note in spec.notes)
    assert spec.confidence < 1.0


def test_non_python_target_is_robust(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Project\n")

    spec = PerceptionPass().compile(
        "task-doc", "Document the project", repo_root=str(tmp_path), file_constraints=("README.md",)
    )

    statuses = {check["name"]: check["status"] for check in spec.checks}
    assert statuses["targets_exist"] == "pass"
    assert statuses["python_targets_parse"] == "pass"
    assert spec.target_files[0].role == "documentation"


def test_request_path_inference_when_constraints_absent(tmp_path: Path) -> None:
    _package(tmp_path)

    spec = PerceptionPass().compile(
        "task-infer", "Please update `pkg/core.py` for the new behavior", repo_root=str(tmp_path)
    )

    assert [boundary.path for boundary in spec.target_files] == ["pkg/core.py"]
    assert any("inferred" in note for note in spec.notes)


def test_perception_pass_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_PERCEPTION_PASS", raising=False)
    assert perception_pass_enabled() is False
    monkeypatch.setenv("UC_PERCEPTION_PASS", "1")
    assert perception_pass_enabled() is True
    monkeypatch.setenv("UC_PERCEPTION_PASS", "on")
    assert perception_pass_enabled() is True
    monkeypatch.setenv("UC_PERCEPTION_PASS", "0")
    assert perception_pass_enabled() is False
