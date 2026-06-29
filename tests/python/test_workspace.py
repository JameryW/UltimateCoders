"""Tests for WorkspaceManager remote-sync (Phase 1 MVP).

Covers the new ``ensure_clone`` / fetch-on-acquire / push-on-release path
using a LOCAL bare repo as the remote — no network access required.
Also verifies the local-only path still behaves as before when no
``remote_url`` is configured.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ultimate_coders.agent.workspace import WorkspaceManager

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _git(args: list[str], cwd: str) -> None:
    """Run a git command, asserting success."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"git {args} failed in {cwd}: {result.stderr}"
    )


def _make_bare_remote(tmp_path: Path) -> Path:
    """Create a bare repo with one commit on ``main``."""
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=str(tmp_path))

    # Seed the bare remote with an initial commit via a temp work repo.
    work = tmp_path / "seed"
    _git(["clone", str(remote), str(work)], cwd=str(tmp_path))
    _git(["config", "user.email", "test@test"], cwd=str(work))
    _git(["config", "user.name", "Test"], cwd=str(work))
    (work / "README.md").write_text("# hello\n")
    _git(["add", "."], cwd=str(work))
    _git(["commit", "-m", "initial"], cwd=str(work))
    _git(["push", "origin", "main"], cwd=str(work))
    shutil.rmtree(work)
    return remote


# --------------------------------------------------------------------------- #
# ensure_clone
# --------------------------------------------------------------------------- #

async def test_ensure_clone_noop_without_remote(tmp_path):
    """Local-only mode: ensure_clone is a no-op when remote_url is empty."""
    mgr = WorkspaceManager(project_path=str(tmp_path / "proj"))
    # Should not raise and should not create anything.
    await mgr.ensure_clone()
    assert not (tmp_path / "proj" / ".git").exists()


async def test_ensure_clone_clones_from_remote(tmp_path):
    """ensure_clone clones the bare remote into project_path."""
    remote = _make_bare_remote(tmp_path)
    proj = tmp_path / "proj"

    mgr = WorkspaceManager(
        project_path=str(proj),
        remote_url=str(remote),
        fetch_on_acquire=True,
        push_on_release=True,
        base_branch="main",
    )
    await mgr.ensure_clone()
    assert (proj / ".git").exists()
    assert (proj / "README.md").exists()


async def test_ensure_clone_idempotent(tmp_path):
    """ensure_clone is idempotent — a second call does not re-clone."""
    remote = _make_bare_remote(tmp_path)
    proj = tmp_path / "proj"

    mgr = WorkspaceManager(
        project_path=str(proj),
        remote_url=str(remote),
        base_branch="main",
    )
    await mgr.ensure_clone()
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(proj),
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Second call should be a no-op.
    await mgr.ensure_clone()
    second_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(proj),
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert first_head == second_head


# --------------------------------------------------------------------------- #
# acquire / release with remote sync
# --------------------------------------------------------------------------- #

async def test_acquire_release_pushes_subtask_branch(tmp_path):
    """Full cycle: clone → acquire (fetch) → commit → release (push).

    Verifies the subtask branch is pushed to the remote bare repo.
    """
    remote = _make_bare_remote(tmp_path)
    proj = tmp_path / "proj"

    mgr = WorkspaceManager(
        project_path=str(proj),
        remote_url=str(remote),
        base_branch="main",
        fetch_on_acquire=True,
        push_on_release=True,
    )
    await mgr.ensure_clone()

    handle = await mgr.acquire("subtask-abc123")
    assert handle is not None
    assert handle.branch_name == "uc/subtask/subtask-abc1"
    assert os.path.isdir(handle.worktree_path)

    # Make a commit in the worktree.
    _git(["config", "user.email", "test@test"], cwd=handle.worktree_path)
    _git(["config", "user.name", "Test"], cwd=handle.worktree_path)
    (Path(handle.worktree_path) / "feature.txt").write_text("new work\n")
    _git(["add", "."], cwd=handle.worktree_path)
    _git(["commit", "-m", "subtask work"], cwd=handle.worktree_path)

    result = await mgr.release(handle, merge=True)
    assert result["status"] in ("merged", "no_changes")
    assert result.get("push_status") == "pushed"

    # The subtask branch must exist on the remote bare repo.
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=str(remote),
        capture_output=True,
        text=True,
    ).stdout
    assert "uc/subtask/subtask-abc1" in branches


async def test_local_only_mode_unchanged(tmp_path):
    """When remote_url is empty, acquire/release behave as the legacy path."""
    # Build a local git repo (not bare) to act as project_path.
    proj = tmp_path / "localproj"
    proj.mkdir()
    _git(["init", "-b", "main", str(proj)], cwd=str(tmp_path))
    _git(["config", "user.email", "test@test"], cwd=str(proj))
    _git(["config", "user.name", "Test"], cwd=str(proj))
    (proj / "README.md").write_text("# local\n")
    _git(["add", "."], cwd=str(proj))
    _git(["commit", "-m", "init"], cwd=str(proj))

    mgr = WorkspaceManager(project_path=str(proj), base_branch="main")
    await mgr.ensure_clone()  # no-op

    handle = await mgr.acquire("subtask-local")
    assert handle is not None
    (Path(handle.worktree_path) / "x.txt").write_text("x\n")
    _git(["add", "."], cwd=handle.worktree_path)
    _git(["commit", "-m", "x"], cwd=handle.worktree_path)

    result = await mgr.release(handle, merge=True)
    assert result["status"] == "merged"
    # No push_status key in local mode.
    assert "push_status" not in result
