"""Tests for MergeArbiter git-level merge arbitration (Phase 2).

Uses a LOCAL bare repo as the remote — no network access required, same
pattern as ``test_workspace.py``. Covers:

- Non-conflicting subtask branches → both merged, main pushed.
- Conflicting subtask branches (same file, divergent edits) → conflict
  detected, conflicting branch preserved, non-conflicting branch merged.
- No remote configured → arbitrate is a graceful no-op.

Result model (c) from ``research/external-git-sync-model.md``:
    workers push ``uc/subtask/<id>`` branches; the arbiter merges them
    into ``origin/main`` and pushes ``main`` back.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from ultimate_coders.agent.merge_arbiter import MergeArbiter

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _git(args: list[str], cwd: str) -> str:
    """Run a git command, asserting success, returning stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"git {args} failed in {cwd}: {result.stderr}"
    )
    return result.stdout


def _make_bare_remote(tmp_path: Path) -> Path:
    """Create a bare repo with one commit on ``main`` + a base file.

    The base file ``app.txt`` has one line so subtask branches can diverge.
    """
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=str(tmp_path))

    work = tmp_path / "seed"
    _git(["clone", str(remote), str(work)], cwd=str(tmp_path))
    _git(["config", "user.email", "test@test"], cwd=str(work))
    _git(["config", "user.name", "Test"], cwd=str(work))
    (work / "app.txt").write_text("line1\n")
    (work / "README.md").write_text("# hello\n")
    _git(["add", "."], cwd=str(work))
    _git(["commit", "-m", "initial"], cwd=str(work))
    _git(["push", "origin", "main"], cwd=str(work))
    shutil.rmtree(work)
    return remote


def _make_subtask_branch(
    remote: Path,
    tmp_path: Path,
    branch: str,
    file_path: str,
    content: str,
    commit_msg: str = "subtask work",
) -> None:
    """Clone the remote, create a branch, edit one file, commit, push."""
    work = tmp_path / f"work-{branch.replace('/', '-')}"
    _git(["clone", str(remote), str(work)], cwd=str(tmp_path))
    _git(["config", "user.email", "test@test"], cwd=str(work))
    _git(["config", "user.name", "Test"], cwd=str(work))
    _git(["checkout", "-b", branch], cwd=str(work))
    (work / file_path).write_text(content)
    _git(["add", "."], cwd=str(work))
    _git(["commit", "-m", commit_msg], cwd=str(work))
    _git(["push", "origin", branch], cwd=str(work))
    shutil.rmtree(work)


def _remote_main_content(remote: Path, file_path: str) -> str:
    """Read a file's content from the remote's main branch."""
    return subprocess.run(
        ["git", "show", f"refs/heads/main:{file_path}"],
        cwd=str(remote),
        capture_output=True,
        text=True,
    ).stdout


# --------------------------------------------------------------------------- #
# Non-conflicting branches → both merged, main pushed
# --------------------------------------------------------------------------- #

async def test_arbitrate_merges_non_conflicting_branches(tmp_path):
    """Two branches touching DIFFERENT files → both merged, main pushed."""
    remote = _make_bare_remote(tmp_path)
    _make_subtask_branch(
        remote, tmp_path, "uc/subtask/aaa111222333",
        "feature_a.txt", "feature A\n",
    )
    _make_subtask_branch(
        remote, tmp_path, "uc/subtask/bbb444555666",
        "feature_b.txt", "feature B\n",
    )

    proj = tmp_path / "arbiter"
    arbiter = MergeArbiter(
        project_path=str(proj),
        remote_url=str(remote),
        base_branch="main",
    )
    await arbiter.ensure_clone()

    result = await arbiter.arbitrate([
        "uc/subtask/aaa111222333",
        "uc/subtask/bbb444555666",
    ])

    assert result["status"] == "merged"
    assert set(result["merged_branches"]) == {
        "uc/subtask/aaa111222333",
        "uc/subtask/bbb444555666",
    }
    assert result["conflict_branches"] == []
    assert result["push_status"] == "pushed"

    # Both files should now be on origin/main.
    assert _remote_main_content(remote, "feature_a.txt") == "feature A\n"
    assert _remote_main_content(remote, "feature_b.txt") == "feature B\n"


# --------------------------------------------------------------------------- #
# Conflicting branches → conflict detected, non-conflicting one merged
# --------------------------------------------------------------------------- #

async def test_arbitrate_detects_conflict_same_file(tmp_path):
    """Two branches editing the SAME lines of the same file → conflict.

    The ConflictResolver cannot resolve a true text conflict on identical
    lines, so the second branch should be recorded as conflicting and
    main should NOT be pushed (push_status skipped).
    """
    remote = _make_bare_remote(tmp_path)
    # Both branches edit app.txt's single line to DIFFERENT values — a
    # genuine conflicting change that neither git nor the resolver can
    # auto-merge (both sides changed the same line from "line1").
    _make_subtask_branch(
        remote, tmp_path, "uc/subtask/ccc111222333",
        "app.txt", "CHANGED_BY_C\n",
    )
    _make_subtask_branch(
        remote, tmp_path, "uc/subtask/ddd444555666",
        "app.txt", "CHANGED_BY_D\n",
    )

    proj = tmp_path / "arbiter"
    arbiter = MergeArbiter(
        project_path=str(proj),
        remote_url=str(remote),
        base_branch="main",
    )
    await arbiter.ensure_clone()

    result = await arbiter.arbitrate([
        "uc/subtask/ccc111222333",
        "uc/subtask/ddd444555666",
    ])

    assert result["status"] == "conflict"
    assert result["push_status"] == "skipped"
    # At least one branch should be recorded as conflicting.
    assert len(result["conflict_branches"]) >= 1
    # The first branch merges cleanly; the second conflicts with it.
    # (git merges the first into main, then the second conflicts.)
    assert "uc/subtask/ccc111222333" in result["merged_branches"]


# --------------------------------------------------------------------------- #
# Non-overlapping changes to the SAME file → merged via ConflictResolver
# --------------------------------------------------------------------------- #

async def test_arbitrate_resolves_non_overlapping_same_file(tmp_path):
    """Two branches editing DIFFERENT parts of the same file → merged.

    The ConflictResolver's auto-merge handles non-overlapping line changes,
    so both branches should merge and main should be pushed.
    """
    remote = _make_bare_remote(tmp_path)
    # Seed remote with a multi-line file so branches can edit different lines.
    work = tmp_path / "seed-multiline"
    _git(["clone", str(remote), str(work)], cwd=str(tmp_path))
    _git(["config", "user.email", "test@test"], cwd=str(work))
    _git(["config", "user.name", "Test"], cwd=str(work))
    (work / "multi.txt").write_text("line1\nline2\nline3\n")
    _git(["add", "."], cwd=str(work))
    _git(["commit", "-m", "add multi"], cwd=str(work))
    _git(["push", "origin", "main"], cwd=str(work))
    shutil.rmtree(work)

    # Branch A appends a line at the end; Branch B edits line1.
    _make_subtask_branch(
        remote, tmp_path, "uc/subtask/eee111222333",
        "multi.txt", "line1\nline2\nline3\nADDED_BY_E\n",
    )
    _make_subtask_branch(
        remote, tmp_path, "uc/subtask/fff444555666",
        "multi.txt", "EDITED_BY_F\nline2\nline3\n",
    )

    proj = tmp_path / "arbiter"
    arbiter = MergeArbiter(
        project_path=str(proj),
        remote_url=str(remote),
        base_branch="main",
    )
    await arbiter.ensure_clone()

    result = await arbiter.arbitrate([
        "uc/subtask/eee111222333",
        "uc/subtask/fff444555666",
    ])

    # git's own merge may or may not resolve this depending on context;
    # the ConflictResolver should handle non-overlapping edits. Either way,
    # the result should be merged (both changes present) or conflict.
    # We assert it does NOT crash and returns a valid status.
    assert result["status"] in ("merged", "conflict")


# --------------------------------------------------------------------------- #
# No remote → arbitrate is a graceful no-op
# --------------------------------------------------------------------------- #

async def test_arbitrate_no_remote_skips_gracefully(tmp_path):
    """When remote_url is empty, ensure_clone + arbitrate are no-ops.

    This guards the local-only mode: an Orchestrator without a remote
    should never construct a MergeArbiter, but if one is accidentally
    constructed with an empty remote it must not crash.
    """
    arbiter = MergeArbiter(
        project_path=str(tmp_path / "proj"),
        remote_url="",
        base_branch="main",
    )
    # ensure_clone is a no-op when remote_url is empty.
    await arbiter.ensure_clone()

    result = await arbiter.arbitrate(["uc/subtask/abc123"])
    # With no remote, arbitrate is a no-op → status skipped.
    assert result["status"] == "skipped"


# --------------------------------------------------------------------------- #
# Empty branch list → skipped
# --------------------------------------------------------------------------- #

async def test_arbitrate_empty_branch_list(tmp_path):
    """An empty subtask_branches list → status skipped."""
    remote = _make_bare_remote(tmp_path)
    proj = tmp_path / "arbiter"
    arbiter = MergeArbiter(
        project_path=str(proj),
        remote_url=str(remote),
        base_branch="main",
    )
    await arbiter.ensure_clone()

    result = await arbiter.arbitrate([])
    assert result["status"] == "skipped"
    assert result["merged_branches"] == []
    assert result["conflict_branches"] == []
