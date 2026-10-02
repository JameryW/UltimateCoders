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
import hashlib
import logging
import os
import shutil
import socket
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Repo-level git identity used when no global user config exists (e.g. worker
# containers, CI runners). ``release()`` runs ``git merge --no-edit`` in the
# main clone, which creates a merge commit and hard-fails without an identity
# on Linux whenever the merge is NOT a fast-forward. Setting a repo-scoped
# identity (never global) after clone makes non-ff merges work out of the box.
_WORKER_IDENTITY_EMAIL = "uc-worker@local"
_WORKER_IDENTITY_NAME = "UC Worker"


def subtask_branch(subtask_id: str, *, inference: bool = False) -> str:
    suffix = hashlib.sha256(subtask_id.encode()).hexdigest()[:16] if inference else subtask_id[:12]
    return f"uc/subtask/{suffix}"


@dataclass
class WorkspaceHandle:
    """A workspace allocated for a subtask."""

    workspace_id: str = ""
    branch_name: str = ""
    worktree_path: str = ""
    subtask_id: str = ""
    project_path: str = ""
    status: str = "ready"  # ready | active | merging | completed | failed
    lease_key: str = ""

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
        self._state = None

    async def _lease_update(self, handle: WorkspaceHandle, delivery: dict | None = None) -> None:
        if self._state and handle.lease_key:
            await asyncio.to_thread(
                self._state.mutate,
                "workspace_leases",
                handle.lease_key,
                lambda old: {
                    **old, "handle": asdict(handle), "status": handle.status,
                    **({"delivery": delivery} if delivery is not None else {}),
                },
            )

    async def quarantine(self, handle: WorkspaceHandle) -> None:
        handle.status = "cleanup_pending"
        await self._lease_update(handle)

    def bind_runtime_state(self, state) -> None:
        """Worker checkpoints and workspace leases must share one state store."""
        if (self._state is not None
                and (self._state.url, self._state.path) != (state.url, state.path)):
            raise RuntimeError("Workspace and Worker runtime state stores differ")
        self._state = state

    async def runner_owner(self, working_dir: str) -> dict | None:
        """Give the runner the current lease token, outside stable experiment identity."""
        if self._state is None:
            return None
        handle = next((item for item in self._active.values()
                       if item.lease_key and item.worktree_path == working_dir), None)
        if handle is None:
            return None
        lease = await asyncio.to_thread(self._state.get, "workspace_leases", handle.lease_key)
        return {"lease_key": handle.lease_key, "claim": lease["claim"]}

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

        A repo-level git identity (``user.email`` / ``user.name``) is set on
        the clone so ``release()``'s ``git merge --no-edit`` can author merge
        commits without a global git config (worker containers typically have
        none). Repo-scoped, overwrites stale values, never touches global
        config. Non-fatal on failure (logged at debug).
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
            await self._ensure_local_identity()
            return

        # Repo already exists — ensure origin points at the configured remote.
        cur = await self._git(["remote", "get-url", self._remote_name])
        if cur["exit_code"] != 0 or cur["stdout"].strip() != self._remote_url:
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

        # (Re)assert the repo-level identity — idempotent. A pre-existing
        # clone may still lack one (e.g. created by an older code path).
        await self._ensure_local_identity()

    async def _ensure_local_identity(self) -> None:
        """Set a repo-level git identity so merge commits can be authored.

        Uses ``git config`` (repo-scoped by default when run inside the repo)
        so the host's global config is never touched. Overwrites any prior
        value, which is safe — the worker owns this clone. Non-fatal: a
        failure is logged at debug and the merge will surface the real error
        if identity was genuinely unavailable.
        """
        for key, value in (
            ("user.email", _WORKER_IDENTITY_EMAIL),
            ("user.name", _WORKER_IDENTITY_NAME),
        ):
            res = await self._git(
                ["config", key, value],
                cwd=self._project_path,
            )
            if res["exit_code"] != 0:
                logger.debug(
                    "ensure_clone: git config %s failed (non-fatal): %s",
                    key, res["stderr"][:200],
                )

    async def acquire(
        self, subtask_id: str, *, require_worktree: bool = False
    ) -> WorkspaceHandle | None:
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
                self._max_worktrees,
                subtask_id[:8],
            )
            return None

        lease_key = ""
        if require_worktree:
            from ultimate_coders.runtime_state import (
                RuntimeState,
                process_identity,
                process_matches,
            )

            if self._state is None:
                self._state = await asyncio.to_thread(
                    RuntimeState,
                    Path(os.environ.get("UC_RUNTIME_STATE_DIR") or
                         Path(self._project_path).resolve() / ".uc/runtime") / "state.sqlite3",
                )
            lease_key = hashlib.sha256(
                (str(Path(self._project_path).resolve()) + ":" + subtask_id).encode()
            ).hexdigest()
            claim = uuid.uuid4().hex
            experiments = await asyncio.to_thread(self._state.records, "experiments")

            def claim_lease(old):
                if old and old.get("status") != "completed":
                    if (
                        old.get("status") in ("cleanup_pending", "failed")
                        or old.get("host") != socket.gethostname()
                    ):
                        return old
                    if old.get("runner_pid") and (
                        old.get("runner_host") != socket.gethostname()
                        or process_matches(old["runner_pid"], old.get("runner_process_identity"))
                    ):
                        return old
                    if process_matches(old.get("pid", os.getpid()), old.get("process_identity")):
                        return old
                    for experiment in experiments:
                        if (experiment.get("workspace")
                                == old.get("handle", {}).get("worktree_path")):
                            if (experiment.get("owner_host") != socket.gethostname()
                                or process_matches(experiment.get("owner_pid", os.getpid()),
                                                   experiment.get("owner_process_identity"))):
                                return old
                return {
                    **old,
                    "claim": claim,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "process_identity": process_identity(os.getpid()),
                    "status": "active",
                }

            lease = await asyncio.to_thread(
                self._state.mutate, "workspace_leases", lease_key, claim_lease
            )
            if lease.get("claim") != claim:
                return None
            if lease.get("handle") and lease["handle"].get("status") != "completed":
                recovered = WorkspaceHandle(**lease["handle"])
                if Path(recovered.worktree_path).is_dir():
                    recovered.status = "active"
                    self._active[recovered.workspace_id] = recovered
                    return recovered

        ws_id = f"ws-{uuid.uuid4().hex[:8]}"
        branch_name = subtask_branch(subtask_id, inference=require_worktree)

        handle = WorkspaceHandle(
            workspace_id=ws_id,
            branch_name=branch_name,
            worktree_path="",  # set after worktree add
            subtask_id=subtask_id,
            project_path=self._project_path,
            status="active",
            lease_key=lease_key,
        )
        await self._lease_update(handle)

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
                        self._remote_name,
                        self._base_branch,
                        self._base_branch,
                        fetch_result["stderr"][:200],
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
                        subtask_id[:8],
                        result["stderr"][:200],
                    )
                    if require_worktree:
                        handle.status = "completed"
                        await self._lease_update(handle)
                        return None
                    # Fallback: use in-project temp directory
                    handle.worktree_path = os.path.join(
                        self._project_path,
                        f".uc/workspaces/{ws_id}",
                    )
                    await self._mkdir(handle.worktree_path)
                    # Copy project files
                    await self._copy_project(handle.worktree_path)
                    # worktree add failed → no git branch was created. Clear
                    # branch_name so release skips merge/push (which would
                    # run `git log`/`git push` against a non-existent branch
                    # and mislabel as no_changes).
                    handle.branch_name = ""
            else:
                handle.worktree_path = os.path.join(
                    self._project_path,
                    f".uc/worktrees/{ws_id}",
                )

            self._active[ws_id] = handle
            if handle.branch_name:
                handle.worktree_path = os.path.join(self._project_path, f".uc/worktrees/{ws_id}")
            await self._lease_update(handle)
            logger.info(
                "Workspace %s acquired for subtask %s (branch=%s)",
                ws_id,
                subtask_id[:8],
                branch_name,
            )
            return handle

        except Exception as e:
            logger.error("Workspace acquisition failed: %s", e, exc_info=True)
            handle.status = "failed"
            await self._lease_update(handle)
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
            # UC owns the commit after validation. Inference candidates deliberately
            # leave accepted edits unstaged; merging only pre-existing commits lost them.
            dirty = await self._git(["status", "--porcelain"], cwd=handle.worktree_path)
            if dirty["exit_code"]:
                handle.status = "failed"
                await self._lease_update(handle)
                return {
                    **result_info,
                    "status": "commit_failed",
                    "branch_preserved": handle.branch_name,
                }
            if dirty["stdout"].strip():
                staged = await self._git(["add", "--all", "--", "."], cwd=handle.worktree_path)
                committed = (
                    await self._git(
                        [
                            "-c",
                            f"user.name={_WORKER_IDENTITY_NAME}",
                            "-c",
                            f"user.email={_WORKER_IDENTITY_EMAIL}",
                            "commit",
                            "-m",
                            f"UC accepted subtask {handle.subtask_id}",
                        ],
                        cwd=handle.worktree_path,
                    )
                    if staged["exit_code"] == 0
                    else staged
                )
                if committed["exit_code"]:
                    handle.status = "failed"
                    await self._lease_update(handle)
                    return {
                        **result_info,
                        "status": "commit_failed",
                        "branch_preserved": handle.branch_name,
                    }
            head = await self._git(["rev-parse", "HEAD"], cwd=handle.worktree_path)
            result_info["commit_sha"] = head["stdout"].strip()
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
                        handle.workspace_id,
                        handle.branch_name,
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
                        "push",
                        self._remote_name,
                        f"{handle.branch_name}:refs/heads/{handle.branch_name}",
                    ],
                    cwd=self._project_path,
                )
                if push_result["exit_code"] != 0:
                    logger.warning(
                        "release: push of branch %s failed (non-fatal): %s",
                        handle.branch_name,
                        push_result["stderr"][:200],
                    )
                    result_info["push_status"] = "failed"
                    result_info["push_error"] = push_result["stderr"][:200]
                else:
                    result_info["push_status"] = "pushed"

        if result_info.get("status") == "conflict" or result_info.get("push_status") == "failed":
            handle.status = "failed"
            await self._lease_update(handle, result_info)
            return result_info

        # Delivery survives a crash between worktree removal and the Worker's
        # final checkpoint. Recovery must not recreate and rerun the candidate.
        handle.status = "delivered"
        await self._lease_update(handle, result_info)
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
        await self._lease_update(handle)
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

        if self._state is None:
            from ultimate_coders.runtime_state import RuntimeState

            self._state = await asyncio.to_thread(
                RuntimeState,
                Path(os.environ.get("UC_RUNTIME_STATE_DIR") or
                     Path(self._project_path).resolve() / ".uc/runtime") / "state.sqlite3",
            )
        leases = await asyncio.to_thread(self._state.records, "workspace_leases")

        for entry in os.listdir(worktrees_dir):
            path = os.path.join(worktrees_dir, entry)
            Path(path).resolve().relative_to(Path(worktrees_dir).resolve())
            if not os.path.isdir(path):
                continue
            # ponytail: simple check — remove if not in active handles
            is_active = any(
                h.worktree_path == path and h.status != "completed" for h in self._active.values()
            )
            if self._state:
                is_active = is_active or any(
                    item.get("status") not in ("completed", "delivered")
                    and item.get("handle", {}).get("worktree_path") == path
                    for item in leases
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
