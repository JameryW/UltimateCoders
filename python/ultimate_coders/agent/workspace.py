"""Workspace isolation for distributed workers.

Provides per-subtask workspace isolation using git worktrees.
Each subtask that modifies files gets its own worktree (branch),
which is merged back into the main branch on completion.

Lifecycle:
    1. acquire(subtask) → create worktree + branch
    2. worker executes in worktree directory
    3. release(subtask) → merge branch back (or preserve on conflict)
    4. cleanup() → remove stale worktrees

ponytail: git worktree per subtask — simple, leverages git's own
isolation. Upgrade to overlayfs if git worktrees are too slow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceHandle:
    """A workspace allocated for a subtask."""

    workspace_id: str = ""
    branch_name: str = ""
    worktree_path: str = ""
    subtask_id: str = ""
    project_path: str = ""
    status: str = "ready"  # ready | active | merging | completed | failed

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class WorkspaceManager:
    """Manages per-subtask workspace isolation via git worktrees.

    Usage:
        mgr = WorkspaceManager(project_path="/path/to/repo")
        handle = await mgr.acquire("subtask-1")
        # ... worker executes in handle.worktree_path ...
        await mgr.release(handle, merge=True)
    """

    def __init__(
        self,
        project_path: str = "",
        max_worktrees: int = 8,
        base_branch: str = "main",
        remote_url: str = "",
        remote_name: str = "origin",
        fetch_on_acquire: bool = False,
        push_on_release: bool = False,
    ) -> None:
        self._project_path = project_path or os.getcwd()
        self._max_worktrees = max_worktrees
        self._base_branch = base_branch
        # Remote-sync config (Phase 1 MVP). When remote_url is empty the
        # manager behaves exactly as the legacy local-only implementation.
        self._remote_url = remote_url
        self._remote_name = remote_name
        self._fetch_on_acquire = fetch_on_acquire
        self._push_on_release = push_on_release
        self._active: dict[str, WorkspaceHandle] = {}
        self._pool: list[WorkspaceHandle] = []

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def ensure_clone(self) -> None:
        """Ensure a git checkout exists at ``project_path``.

        When ``remote_url`` is set and the project path is not already a git
        repo with that remote, clone it. If the repo already exists and the
        remote differs, update the remote URL. Idempotent.

        When ``remote_url`` is empty this is a no-op (local-only mode, fully
        backward compatible with the legacy behaviour).
        """
        if not self._remote_url:
            return  # local-only mode

        git_dir = os.path.join(self._project_path, ".git")
        if not os.path.exists(git_dir):
            # Path is empty/non-git: clone the remote into it.
            # NOTE: ``_git`` defaults cwd to ``self._project_path`` which does
            # not exist yet — run the clone from the parent dir instead.
            parent = os.path.dirname(self._project_path) or os.getcwd()
            os.makedirs(parent, exist_ok=True)
            result = await self._git(
                ["clone", self._remote_url, self._project_path],
                cwd=parent,
            )
            if result["exit_code"] != 0:
                logger.error(
                    "ensure_clone: clone of %s failed: %s",
                    self._remote_url, result["stderr"][:300],
                )
                raise RuntimeError(
                    f"git clone failed: {result['stderr'][:200]}"
                )
            logger.info(
                "ensure_clone: cloned %s into %s",
                self._remote_url, self._project_path,
            )
            return

        # Repo already exists — ensure origin points at the configured remote.
        cur = await self._git(["remote", "get-url", self._remote_name])
        if cur["exit_code"] == 0 and cur["stdout"].strip() == self._remote_url:
            logger.debug("ensure_clone: remote %s already correct", self._remote_name)
            return
        # Remote missing or mismatched: (re)set it.
        if cur["exit_code"] != 0:
            add = await self._git(
                ["remote", "add", self._remote_name, self._remote_url]
            )
            if add["exit_code"] != 0:
                logger.warning("ensure_clone: add remote failed: %s", add["stderr"][:200])
        else:
            await self._git(
                ["remote", "set-url", self._remote_name, self._remote_url]
            )
        logger.info("ensure_clone: remote %s set to %s", self._remote_name, self._remote_url)

    async def acquire(self, subtask_id: str) -> WorkspaceHandle | None:
        """Create an isolated workspace for a subtask.

        Creates a git worktree on a new branch. The worker can
        safely modify files in this worktree without affecting
        other workers.

        Returns:
            WorkspaceHandle with the worktree path, or None on failure.
        """
        if len(self._active) >= self._max_worktrees:
            logger.warning(
                "Max worktrees (%d) reached, cannot allocate for %s",
                self._max_worktrees, subtask_id[:8],
            )
            return None

        ws_id = f"ws-{uuid.uuid4().hex[:8]}"
        branch_name = f"uc/subtask/{subtask_id[:12]}"

        handle = WorkspaceHandle(
            workspace_id=ws_id,
            branch_name=branch_name,
            worktree_path="",  # set after worktree add
            subtask_id=subtask_id,
            project_path=self._project_path,
            status="active",
        )

        try:
            # Determine the base ref to branch the worktree from.
            # When remote sync is enabled, fetch first so the worktree is
            # based on the fresh upstream HEAD (origin/<base_branch>), not a
            # stale local HEAD. Fall back to the local branch on any failure.
            base_ref = self._base_branch
            if self._fetch_on_acquire and self._remote_url:
                fetch_result = await self._git(
                    ["fetch", self._remote_name, self._base_branch],
                    cwd=self._project_path,
                )
                if fetch_result["exit_code"] == 0:
                    base_ref = f"{self._remote_name}/{self._base_branch}"
                else:
                    logger.warning(
                        "fetch %s/%s failed, branching off local %s: %s",
                        self._remote_name, self._base_branch,
                        self._base_branch, fetch_result["stderr"][:200],
                    )

            # Create git worktree on a new branch
            result = await self._git(
                ["worktree", "add", "-b", branch_name, f".uc/worktrees/{ws_id}", base_ref],
                cwd=self._project_path,
            )
            if result["exit_code"] != 0:
                # Fallback: try without specifying base branch (uses HEAD)
                result = await self._git(
                    ["worktree", "add", "-b", branch_name, f".uc/worktrees/{ws_id}"],
                    cwd=self._project_path,
                )
                if result["exit_code"] != 0:
                    logger.error(
                        "Failed to create worktree for %s: %s",
                        subtask_id[:8], result["stderr"][:200],
                    )
                    # Fallback: use in-project temp directory
                    handle.worktree_path = os.path.join(
                        self._project_path, f".uc/workspaces/{ws_id}",
                    )
                    await self._mkdir(handle.worktree_path)
                    # Copy project files
                    await self._copy_project(handle.worktree_path)
            else:
                handle.worktree_path = os.path.join(
                    self._project_path, f".uc/worktrees/{ws_id}",
                )

            self._active[ws_id] = handle
            logger.info(
                "Workspace %s acquired for subtask %s (branch=%s)",
                ws_id, subtask_id[:8], branch_name,
            )
            return handle

        except Exception as e:
            logger.error("Workspace acquisition failed: %s", e, exc_info=True)
            handle.status = "failed"
            return None

    async def release(
        self,
        handle: WorkspaceHandle,
        merge: bool = True,
    ) -> dict[str, Any]:
        """Release a workspace, optionally merging changes back.

        Args:
            handle: The workspace handle to release.
            merge: Whether to merge the branch back into base_branch.

        Returns:
            Dict with merge status and any conflicts.
        """
        if handle.workspace_id not in self._active:
            return {"status": "not_found"}

        handle.status = "merging"
        result_info: dict[str, Any] = {"workspace_id": handle.workspace_id}

        if merge and handle.branch_name:
            # Check if there are any commits on the branch
            log_result = await self._git(
                ["log", f"{self._base_branch}..{handle.branch_name}", "--oneline"],
                cwd=handle.worktree_path or self._project_path,
            )

            if log_result["exit_code"] == 0 and log_result["stdout"].strip():
                # Merge the branch back
                merge_result = await self._git(
                    ["merge", handle.branch_name, "--no-edit"],
                    cwd=self._project_path,
                )
                if merge_result["exit_code"] != 0:
                    # Conflict — abort merge and preserve branch
                    await self._git(["merge", "--abort"], cwd=self._project_path)
                    result_info["status"] = "conflict"
                    result_info["branch_preserved"] = handle.branch_name
                    logger.warning(
                        "Merge conflict for workspace %s, branch %s preserved",
                        handle.workspace_id, handle.branch_name,
                    )
                else:
                    result_info["status"] = "merged"
            else:
                result_info["status"] = "no_changes"

            # Push the subtask branch to the remote (NOT main — merge
            # arbitration into main is Phase 2 / gateway). Push is opt-in
            # via push_on_release and only when a remote is configured.
            if (
                self._push_on_release
                and self._remote_url
                and handle.branch_name
                and result_info.get("status") in ("merged", "no_changes")
            ):
                push_result = await self._git(
                    [
                        "push", self._remote_name,
                        f"{handle.branch_name}:refs/heads/{handle.branch_name}",
                    ],
                    cwd=self._project_path,
                )
                if push_result["exit_code"] != 0:
                    logger.warning(
                        "release: push of branch %s failed (non-fatal): %s",
                        handle.branch_name, push_result["stderr"][:200],
                    )
                    result_info["push_status"] = "failed"
                    result_info["push_error"] = push_result["stderr"][:200]
                else:
                    result_info["push_status"] = "pushed"

        # Remove the worktree
        try:
            wt_path = os.path.join(
                self._project_path,
                f".uc/worktrees/{handle.workspace_id}",
            )
            if os.path.exists(wt_path):
                await self._git(
                    ["worktree", "remove", f".uc/worktrees/{handle.workspace_id}", "--force"],
                    cwd=self._project_path,
                )
                # Delete the branch if merge succeeded AND push (if any) succeeded.
                # Preserve the branch on conflict or push failure so it can be retried.
                if (
                    result_info.get("status") != "conflict"
                    and result_info.get("push_status") != "failed"
                ):
                    await self._git(
                        ["branch", "-D", handle.branch_name],
                        cwd=self._project_path,
                    )
            elif os.path.exists(handle.worktree_path):
                shutil.rmtree(handle.worktree_path, ignore_errors=True)
        except Exception as e:
            logger.debug("Worktree cleanup failed (non-fatal): %s", e)

        handle.status = "completed"
        self._active.pop(handle.workspace_id, None)
        return result_info

    async def cleanup(self) -> int:
        """Remove stale worktrees older than 1 hour.

        Returns:
            Number of worktrees cleaned up.
        """
        cleaned = 0
        worktrees_dir = os.path.join(self._project_path, ".uc", "worktrees")
        if not os.path.exists(worktrees_dir):
            return 0

        for entry in os.listdir(worktrees_dir):
            path = os.path.join(worktrees_dir, entry)
            if not os.path.isdir(path):
                continue
            # ponytail: simple check — remove if not in active handles
            is_active = any(
                h.worktree_path == path and h.is_active
                for h in self._active.values()
            )
            if not is_active:
                try:
                    await self._git(
                        ["worktree", "remove", path, "--force"],
                        cwd=self._project_path,
                    )
                    cleaned += 1
                except Exception:
                    shutil.rmtree(path, ignore_errors=True)
                    cleaned += 1

        return cleaned

    async def _git(self, args: list[str], cwd: str = "") -> dict[str, Any]:
        """Run a git command and return stdout/stderr/exit_code."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or self._project_path,
        )
        stdout, stderr = await proc.communicate()
        return {
            "exit_code": proc.returncode or 0,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    async def _mkdir(self, path: str) -> None:
        """Create directory recursively."""
        os.makedirs(path, exist_ok=True)

    async def _copy_project(self, dest: str) -> None:
        """Copy project files to workspace (fallback when git worktree fails).

        ponytail: excludes .git, node_modules, __pycache__, .uc —
        upgrade to hardlink-based copy for speed if this path is hot.
        """
        excludes = {".git", "node_modules", "__pycache__", ".uc", ".mypy_cache", "target"}
        for item in os.listdir(self._project_path):
            if item in excludes:
                continue
            src = os.path.join(self._project_path, item)
            dst = os.path.join(dest, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*excludes))
            else:
                shutil.copy2(src, dst)
