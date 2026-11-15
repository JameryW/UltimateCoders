"""Attempt evidence capture — ``WorkspaceManager.capture_diff``.

The ledger describes *what an attempt actually changed*, so capture must see
committed and uncommitted edits from the branch fork point, synthesize patches
for untracked files (git diff ignores them), and stay bounded. It must also be
read-only: capture runs before ``release()`` and must not alter delivery.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from ultimate_coders.agent.workspace import WorkspaceHandle, WorkspaceManager

pytestmark = pytest.mark.asyncio


def _git(args: list[str], cwd: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"


def _remove_tree(path: Path) -> None:
    def _make_writable_and_retry(func, target, _exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_make_writable_and_retry)


def _make_repo(tmp_path: Path) -> str:
    """Clone a seeded bare remote so worktrees branch off ``origin/main``."""
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=str(tmp_path))
    seed = tmp_path / "seed"
    _git(["clone", str(remote), str(seed)], cwd=str(tmp_path))
    _git(["config", "user.email", "test@test"], cwd=str(seed))
    _git(["config", "user.name", "Test"], cwd=str(seed))
    (seed / "README.md").write_text("# hello\n")
    (seed / "obsolete.txt").write_text("remove me\n")
    _git(["add", "."], cwd=str(seed))
    _git(["commit", "-m", "initial"], cwd=str(seed))
    _git(["push", "origin", "main"], cwd=str(seed))
    _remove_tree(seed)

    return str(remote)


async def _acquire(tmp_path: Path):
    manager = WorkspaceManager(str(tmp_path / "project"), remote_url=str(tmp_path / "remote.git"))
    await manager.ensure_clone()
    handle = await manager.acquire("attempt-evidence")
    assert handle is not None
    return manager, handle


async def test_capture_diff_records_modified_created_deleted(tmp_path):
    _make_repo(tmp_path)
    manager, handle = await _acquire(tmp_path)
    worktree = Path(handle.worktree_path)
    (worktree / "README.md").write_text("# hello updated\n")
    (worktree / "new_module.py").write_text("VALUE = 1\n")
    (worktree / "obsolete.txt").unlink()

    snapshot = await manager.capture_diff(handle, max_files=20, max_diff_chars=20000)

    assert snapshot["baseline"]
    assert snapshot["head"] == await manager.head_sha(handle)
    by_path = {entry["path"]: entry for entry in snapshot["files"]}
    assert by_path["README.md"]["change_type"] == "modified"
    assert "+# hello updated" in by_path["README.md"]["diff"]
    assert by_path["new_module.py"]["change_type"] == "created"
    # git diff does not show untracked files; capture synthesizes the patch.
    assert "+VALUE = 1" in by_path["new_module.py"]["diff"]
    assert by_path["obsolete.txt"]["change_type"] == "deleted"
    assert "-remove me" in by_path["obsolete.txt"]["diff"]
    assert snapshot["file_count"] == 3
    assert snapshot["truncated"] is False
    assert await manager.release(handle, merge=True)


async def test_capture_diff_bounds_files_and_characters(tmp_path):
    _make_repo(tmp_path)
    manager, handle = await _acquire(tmp_path)
    worktree = Path(handle.worktree_path)
    for index in range(5):
        (worktree / f"file_{index}.py").write_text(f"VALUE = {index}\n")

    snapshot = await manager.capture_diff(handle, max_files=2, max_diff_chars=20000)

    assert snapshot["file_count"] == 5
    assert len(snapshot["files"]) == 2
    assert snapshot["truncated"] is True
    assert await manager.release(handle, merge=True)


async def test_capture_diff_is_empty_without_a_worktree(tmp_path):
    manager = WorkspaceManager(str(tmp_path / "project"))
    handle = WorkspaceHandle(workspace_id="w1", worktree_path="")

    snapshot = await manager.capture_diff(handle)

    assert snapshot == {
        "baseline": "",
        "head": "",
        "files": [],
        "file_count": 0,
        "truncated": False,
    }


async def test_capture_diff_does_not_stage_or_commit(tmp_path):
    _make_repo(tmp_path)
    manager, handle = await _acquire(tmp_path)
    worktree = Path(handle.worktree_path)
    (worktree / "README.md").write_text("# changed\n")

    await manager.capture_diff(handle)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0
    assert " M README.md" in status.stdout  # still unstaged, uncommitted
    assert await manager.release(handle, merge=True)
