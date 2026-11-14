"""Workspace cleanup must really remove what it counts, and prune dead metadata (T67).

`cleanup()` counted a worktree as cleaned whenever `git worktree remove` returned — and its
`except Exception` fallback could never run, because `_git` returns instead of raising. When
git *refused* (the directory's `.git` file is gone after a crash, or a file is locked) the
directory stayed, git kept the worktree registered, and the subtask branch stayed pinned:
`git branch -D` answers "used by worktree at ...". Cleanup reported success every time.

These tests drive a real repository — the defect is in how git and the filesystem are
reconciled, so a mock would only re-assert the code under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from ultimate_coders.agent.workspace import WorkspaceHandle, WorkspaceManager

GIT_TIMEOUT = 60


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def _worktree_entries(repo: str) -> list[str]:
    listed = _git(repo, "worktree", "list", "--porcelain").stdout
    return [line for line in listed.splitlines() if line.startswith("worktree ")]


@pytest.fixture()
def repo(tmp_path) -> str:
    path = str(tmp_path / "repo")
    os.makedirs(path)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@local")
    with open(os.path.join(path, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("a\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def _add_worktree(repo: str, name: str, branch: str) -> str:
    path = os.path.join(repo, ".uc", "worktrees", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    result = _git(repo, "worktree", "add", "-b", branch, path)
    assert result.returncode == 0, result.stderr
    return path


async def test_cleanup_really_removes_a_worktree_whose_git_file_vanished(repo):
    worktree = _add_worktree(repo, "ws1", "uc/subtask/ws1")
    # A crash between the directory removal and git's own bookkeeping leaves a dir
    # that is not a valid worktree any more: `git worktree remove` refuses it.
    shutil.rmtree(worktree)
    os.makedirs(worktree)

    manager = WorkspaceManager(project_path=repo)
    cleaned = await manager.cleanup()

    assert cleaned == 1
    assert not os.path.isdir(worktree), "cleanup reported a removal it did not perform"
    assert len(_worktree_entries(repo)) == 1, "git still lists the dead worktree"
    assert _git(repo, "branch", "-D", "uc/subtask/ws1").returncode == 0, (
        "the branch stayed pinned to a worktree that no longer exists"
    )


async def test_cleanup_prunes_metadata_when_the_directory_is_gone(repo):
    worktree = _add_worktree(repo, "ws2", "uc/subtask/ws2")
    shutil.rmtree(worktree)
    assert len(_worktree_entries(repo)) == 2

    manager = WorkspaceManager(project_path=repo)
    await manager.cleanup()

    assert len(_worktree_entries(repo)) == 1, "stale metadata was never pruned"
    assert _git(repo, "branch", "-D", "uc/subtask/ws2").returncode == 0


async def test_cleanup_leaves_an_active_worktree_alone(repo):
    worktree = _add_worktree(repo, "ws3", "uc/subtask/ws3")
    manager = WorkspaceManager(project_path=repo)
    handle = WorkspaceHandle(
        workspace_id="ws3",
        branch_name="uc/subtask/ws3",
        worktree_path=worktree,
        status="active",
    )
    manager._active["ws3"] = handle

    cleaned = await manager.cleanup()

    assert cleaned == 0
    assert os.path.isdir(worktree)
    assert len(_worktree_entries(repo)) == 2, "an active worktree must keep its metadata"
