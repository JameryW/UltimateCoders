"""Tests for the SWE-Shift guard wiring in the local harness (v29 #26).

The guard freezes writes after the workspace changes underneath an attempt
(collaborator commit, user edit, checkout) until the agent re-reads the paths
that moved.  These tests cover the git snapshot helper and the harness wiring;
the state machine itself is covered by ``test_v11_shift_guard.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ultimate_coders.agent import harness_local_loop as hll
from ultimate_coders.agent.shift_guard import (
    UC_SHIFT_GUARD,
    ShiftGuard,
    ShiftPhase,
    WorkspaceEpoch,
    WorkspaceSnapshot,
    snapshot_workspace,
)


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


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


class TestSnapshot:
    def test_clean_repo_snapshot(self, repo: Path) -> None:
        snapshot = snapshot_workspace(repo)
        assert snapshot is not None
        assert snapshot.changed_paths == ()
        assert snapshot.commit
        assert snapshot.epoch.git_head == snapshot.commit

    def test_dirty_repo_records_changed_paths(self, repo: Path) -> None:
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        snapshot = snapshot_workspace(repo)
        assert snapshot is not None
        assert snapshot.changed_paths == ("app.py",)
        assert snapshot.epoch.git_head.startswith(snapshot.commit + "+")

    def test_edit_changes_the_epoch(self, repo: Path) -> None:
        clean = snapshot_workspace(repo)
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        dirty = snapshot_workspace(repo)
        assert clean is not None and dirty is not None
        assert clean.epoch != dirty.epoch

    def test_commit_changes_the_epoch(self, repo: Path) -> None:
        before = snapshot_workspace(repo)
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "collaborator")
        after = snapshot_workspace(repo)
        assert before is not None and after is not None
        assert before.epoch != after.epoch
        assert after.changed_paths == ()

    def test_untracked_files_are_ignored(self, repo: Path) -> None:
        (repo / "scratch.txt").write_text("noise\n", encoding="utf-8")
        snapshot = snapshot_workspace(repo)
        assert snapshot is not None
        assert snapshot.changed_paths == ()

    def test_non_repo_returns_none(self, tmp_path: Path) -> None:
        assert snapshot_workspace(tmp_path) is None

    def test_renames_report_the_new_path(self, repo: Path) -> None:
        _git(repo, "mv", "app.py", "main.py")
        snapshot = snapshot_workspace(repo)
        assert snapshot is not None
        assert "main.py" in snapshot.changed_paths


class TestShiftGuardIntegration:
    def test_write_freezes_until_the_changed_path_is_reread(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(UC_SHIFT_GUARD, "1")
        base = WorkspaceSnapshot(WorkspaceEpoch(git_head="c0"), (), "c0")
        shifted = WorkspaceSnapshot(WorkspaceEpoch(git_head="c1+deadbeef"), ("app.py",), "c1")
        # Creation reads the base epoch; every later probe reports the shift.
        probes = iter([base] + [shifted] * 8)
        responses = iter(
            [
                _resp([("write_file", {"path": "app.py", "content": "print('v2')\n"})]),
                _resp([("read_file", {"path": "app.py"})]),
                _resp([("write_file", {"path": "app.py", "content": "print('v2')\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with (
            patch.object(hll, "_completion", side_effect=lambda m: next(responses)),
            patch.object(hll, "snapshot_workspace", side_effect=lambda root: next(probes)),
        ):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        blocks = [ln for ln in lines if ln.get("event") == "shift_block"]
        assert [row["tool"] for row in blocks] == ["write_file"]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["shift"]["events"]
        assert final["shift"]["remaining"] == []
        assert (repo / "app.py").read_text(encoding="utf-8") == "print('v2')\n"

    def test_recovery_requires_every_changed_path(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(UC_SHIFT_GUARD, "1")
        (repo / "a.py").write_text("a\n", encoding="utf-8")
        (repo / "b.py").write_text("b\n", encoding="utf-8")
        base = WorkspaceSnapshot(WorkspaceEpoch(git_head="c0"), (), "c0")
        shifted = WorkspaceSnapshot(WorkspaceEpoch(git_head="c1+feedface"), ("a.py", "b.py"), "c1")
        probes = iter([base] + [shifted] * 8)
        responses = iter(
            [
                _resp([("write_file", {"path": "c.py", "content": "x\n"})]),
                _resp([("read_file", {"path": "a.py"})]),
                _resp([("write_file", {"path": "c.py", "content": "x\n"})]),
                _resp([("read_file", {"path": "b.py"})]),
                _resp([("write_file", {"path": "c.py", "content": "x\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with (
            patch.object(hll, "_completion", side_effect=lambda m: next(responses)),
            patch.object(hll, "snapshot_workspace", side_effect=lambda root: next(probes)),
        ):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        assert len([ln for ln in lines if ln.get("event") == "shift_block"]) == 2

    def test_unchanged_workspace_never_blocks(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(UC_SHIFT_GUARD, "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "app.py", "content": "print('v2')\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        assert [ln for ln in lines if ln.get("event") == "shift_block"] == []
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["shift"]["events"] == []

    def test_without_git_the_guard_stays_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(UC_SHIFT_GUARD, "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "app.py", "content": "x\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert "shift" not in final

    def test_disabled_flag_keeps_the_legacy_loop(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.delenv(UC_SHIFT_GUARD, raising=False)
        responses = iter(
            [
                _resp([("write_file", {"path": "app.py", "content": "print('v2')\n"})]),
                _resp([("done", {"summary": "written"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        assert [ln for ln in lines if ln.get("event") == "shift_block"] == []
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert "shift" not in final


class TestWriteGate:
    def test_gate_returns_none_when_stable(self, repo: Path) -> None:
        snapshot = snapshot_workspace(repo)
        assert snapshot is not None
        guard = ShiftGuard(initial_epoch=snapshot.epoch)
        assert hll._shift_write_gate(guard, repo, "app.py") is None

    def test_gate_payload_names_the_targets(self, repo: Path) -> None:
        snapshot = snapshot_workspace(repo)
        assert snapshot is not None
        guard = ShiftGuard(initial_epoch=snapshot.epoch)
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        payload = hll._shift_write_gate(guard, repo, "app.py")
        assert payload is not None
        body = json.loads(payload)
        assert body["error"] == "workspace_changed"
        assert body["must_observe"] == ["app.py"]
        assert body["target"] == "app.py"
        assert guard.phase is ShiftPhase.HISTORY_RESET_REQUIRED

    def test_gate_clears_after_the_observation(self, repo: Path) -> None:
        snapshot = snapshot_workspace(repo)
        assert snapshot is not None
        guard = ShiftGuard(initial_epoch=snapshot.epoch)
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        assert hll._shift_write_gate(guard, repo, "app.py") is not None
        guard.record_fresh_observation("app.py")
        assert hll._shift_write_gate(guard, repo, "app.py") is None
