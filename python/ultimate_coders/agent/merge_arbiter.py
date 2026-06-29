"""Git-level merge arbitration for distributed worker results (Phase 2).

When a task's subtasks complete, the Orchestrator (running in default /
nats-worker consumer mode) fetches each ``uc/subtask/<id>`` branch that
workers pushed to the external remote, merges them into ``origin/main``,
and pushes ``main`` back. This makes the external GitHub/GitLab repo the
unified source of truth across distributed workers.

Result model (c) from ``research/external-git-sync-model.md``:
    - Workers push ONLY their subtask branch (``uc/subtask/<id>``).
    - Workers NEVER touch ``main``.
    - This arbiter is the ONLY writer of ``main`` on the remote.

Conflict handling:
    git's own 3-way merge handles the common case. When ``git merge``
    reports a conflict, the arbiter escalates to the existing
    ``ConflictResolver`` (in-memory 3-way merge on file content) for each
    conflicted file. If the resolver succeeds, the merged content is
    written + ``git add``-ed and the merge continues. If not, the merge
    is aborted, the branch is recorded as conflicting, and ``main`` is
    NOT pushed (conflict state preserved for inspection/retry).

ponytail: git-level merge is authoritative; the in-memory
ResultAggregator (``aggregator.py``) is a separate concern and stays
as-is for its own callers. Phase 3 will demote
DistributedConflictDetector to an advisory scheduling hint.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ultimate_coders.agent.conflict import ConflictResolver, ResolutionTier

logger = logging.getLogger(__name__)


class MergeArbiter:
    """Git-level merge arbiter — merges subtask branches into origin/main.

    Usage::

        arbiter = MergeArbiter(
            project_path="/workspace/repo",
            remote_url="https://github.com/org/repo.git",
            base_branch="main",
        )
        await arbiter.ensure_clone()
        result = await arbiter.arbitrate([
            "uc/subtask/abc123def456",
            "uc/subtask/789abc012def",
        ])
        # result["status"] in {"merged", "conflict", "failed", "skipped"}
    """

    def __init__(
        self,
        project_path: str,
        remote_url: str,
        base_branch: str = "main",
        remote_name: str = "origin",
        llm_client: Any = None,
    ) -> None:
        self._project_path = project_path or os.getcwd()
        self._remote_url = remote_url
        self._base_branch = base_branch
        self._remote_name = remote_name
        # ConflictResolver is the escalation path for text-level conflicts
        # that git's own merge cannot auto-resolve.
        self._resolver = ConflictResolver(llm_client)

    async def ensure_clone(self) -> None:
        """Ensure a git checkout of the remote exists at ``project_path``.

        Same pattern as ``WorkspaceManager.ensure_clone``: clone if the path
        is not yet a git repo, otherwise ensure the remote URL is correct.
        Idempotent. Raises ``RuntimeError`` on clone failure.
        """
        if not self._remote_url:
            return  # local-only mode (should not happen — arbiter is opt-in)

        git_dir = os.path.join(self._project_path, ".git")
        if not os.path.exists(git_dir):
            parent = os.path.dirname(self._project_path) or os.getcwd()
            os.makedirs(parent, exist_ok=True)
            result = await self._git(
                ["clone", self._remote_url, self._project_path],
                cwd=parent,
            )
            if result["exit_code"] != 0:
                logger.error(
                    "MergeArbiter.ensure_clone: clone of %s failed: %s",
                    self._remote_url, result["stderr"][:300],
                )
                raise RuntimeError(
                    f"git clone failed: {result['stderr'][:200]}"
                )
            logger.info(
                "MergeArbiter.ensure_clone: cloned %s into %s",
                self._remote_url, self._project_path,
            )
            return

        # Repo exists — ensure origin points at the configured remote.
        cur = await self._git(["remote", "get-url", self._remote_name])
        if cur["exit_code"] == 0 and cur["stdout"].strip() == self._remote_url:
            return
        if cur["exit_code"] != 0:
            add = await self._git(
                ["remote", "add", self._remote_name, self._remote_url]
            )
            if add["exit_code"] != 0:
                logger.warning(
                    "MergeArbiter.ensure_clone: add remote failed: %s",
                    add["stderr"][:200],
                )
        else:
            await self._git(
                ["remote", "set-url", self._remote_name, self._remote_url]
            )
        logger.info(
            "MergeArbiter.ensure_clone: remote %s set to %s",
            self._remote_name, self._remote_url,
        )

    async def arbitrate(self, subtask_branches: list[str]) -> dict[str, Any]:
        """Merge subtask branches into ``origin/<base_branch>`` and push.

        Steps:
            1. ``git fetch origin <base_branch>`` — get fresh upstream main.
            2. Checkout base_branch, reset to ``origin/<base_branch>``
               (clean main, discard local-only commits).
            3. For each ``uc/subtask/<id>`` branch:
               - fetch the branch ref from origin
               - ``git merge <branch> --no-edit``
               - on conflict: try ConflictResolver on each conflicted file;
                 if all resolved, ``git add`` + continue; else abort merge,
                 record branch as conflicting, move on.
            4. If ALL branches merged cleanly: ``git push origin <base_branch>``.
               On ANY conflict: do NOT push main (preserve conflict state).

        Args:
            subtask_branches: Branch names like ``uc/subtask/<id>``.

        Returns:
            Dict with keys:
                - ``status``: ``merged`` | ``conflict`` | ``failed`` | ``skipped``
                - ``merged_branches``: list[str]
                - ``conflict_branches``: list[str]
                - ``push_status``: ``pushed`` | ``skipped`` | ``failed`` | ""
                - ``error``: str (on failure)
        """
        result: dict[str, Any] = {
            "status": "skipped",
            "merged_branches": [],
            "conflict_branches": [],
            "push_status": "",
            "error": "",
        }

        # No remote configured → local-only mode, arbitration is a no-op.
        if not self._remote_url:
            return result

        if not subtask_branches:
            result["status"] = "skipped"
            return result

        # Step 1: fetch fresh base branch from origin.
        fetch_base = await self._git(
            ["fetch", self._remote_name, self._base_branch],
            cwd=self._project_path,
        )
        if fetch_base["exit_code"] != 0:
            result["status"] = "failed"
            result["error"] = f"fetch base failed: {fetch_base['stderr'][:200]}"
            logger.error("MergeArbiter: %s", result["error"])
            return result

        # Step 2: checkout base branch + reset to origin/base_branch so main
        # is a clean mirror of upstream (arbiter owns main, so no local-only
        # commits should exist — but reset hardens against partial state).
        co = await self._git(
            ["checkout", self._base_branch],
            cwd=self._project_path,
        )
        if co["exit_code"] != 0:
            # Branch may not exist locally yet — create it tracking origin.
            co = await self._git(
                [
                    "checkout", "-b", self._base_branch,
                    f"{self._remote_name}/{self._base_branch}",
                ],
                cwd=self._project_path,
            )
            if co["exit_code"] != 0:
                result["status"] = "failed"
                result["error"] = f"checkout base failed: {co['stderr'][:200]}"
                logger.error("MergeArbiter: %s", result["error"])
                return result

        reset = await self._git(
            ["reset", "--hard", f"{self._remote_name}/{self._base_branch}"],
            cwd=self._project_path,
        )
        if reset["exit_code"] != 0:
            result["status"] = "failed"
            result["error"] = f"reset base failed: {reset['stderr'][:200]}"
            logger.error("MergeArbiter: %s", result["error"])
            return result

        # Step 3: merge each subtask branch.
        merged: list[str] = []
        conflicts: list[str] = []
        for branch in subtask_branches:
            # Fetch the branch ref from origin (no-op if already local).
            await self._git(
                [
                    "fetch", self._remote_name,
                    f"refs/heads/{branch}:refs/heads/{branch}",
                ],
                cwd=self._project_path,
            )

            merge_res = await self._git(
                ["merge", branch, "--no-edit"],
                cwd=self._project_path,
            )
            if merge_res["exit_code"] == 0:
                merged.append(branch)
                logger.info("MergeArbiter: merged %s", branch)
                continue

            # Conflict — attempt ConflictResolver escalation on each
            # conflicted file. If all resolve, stage + continue the merge.
            resolved_all = await self._try_resolve_conflicts()
            if resolved_all:
                # Stage resolved files and finalize the merge with a commit.
                commit_res = await self._git(
                    ["commit", "--no-edit", "--no-verify"],
                    cwd=self._project_path,
                )
                if commit_res["exit_code"] == 0:
                    merged.append(branch)
                    logger.info(
                        "MergeArbiter: merged %s (conflicts resolved via ConflictResolver)",
                        branch,
                    )
                    continue

            # Could not resolve — abort the merge, record the branch.
            await self._git(["merge", "--abort"], cwd=self._project_path)
            conflicts.append(branch)
            logger.warning(
                "MergeArbiter: conflict on %s, merge aborted, branch preserved",
                branch,
            )

        result["merged_branches"] = merged
        result["conflict_branches"] = conflicts

        # Step 4: push main ONLY if all branches merged cleanly.
        if conflicts:
            result["status"] = "conflict"
            result["push_status"] = "skipped"
            return result

        # All merged — push main.
        push_res = await self._git(
            ["push", self._remote_name, self._base_branch],
            cwd=self._project_path,
        )
        if push_res["exit_code"] == 0:
            result["status"] = "merged"
            result["push_status"] = "pushed"
            logger.info(
                "MergeArbiter: pushed %s (%d branches merged)",
                self._base_branch, len(merged),
            )
        else:
            result["status"] = "failed"
            result["push_status"] = "failed"
            result["error"] = f"push failed: {push_res['stderr'][:200]}"
            logger.error("MergeArbiter: %s", result["error"])

        return result

    async def _try_resolve_conflicts(self) -> bool:
        """Attempt to resolve all conflicted files via ConflictResolver.

        Reads the list of conflicted files (``git diff --name-only
        --diff-filter=U``), then for each file reconstructs the 3-way
        inputs (base, ours, theirs) from git's stage versions and runs
        the ConflictResolver. If the resolver succeeds for ALL files,
        writes the merged content + ``git add`` each, returns True.
        Returns False if any file cannot be resolved.
        """
        files_res = await self._git(
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=self._project_path,
        )
        if files_res["exit_code"] != 0:
            return False

        files = [
            f for f in files_res["stdout"].splitlines() if f.strip()
        ]
        if not files:
            return False

        all_resolved = True
        for fpath in files:
            resolved = await self._resolve_one_file(fpath)
            if not resolved:
                all_resolved = False
                break

        return all_resolved

    async def _resolve_one_file(self, fpath: str) -> bool:
        """Resolve a single conflicted file via ConflictResolver.

        Git stores the three stage versions during a conflicted merge:
            - stage 1 (base):   the common ancestor
            - stage 2 (ours):   HEAD (the base_branch side)
            - stage 3 (theirs): the branch being merged

        Uses ``git show :1:<path>`` etc. to read each version, runs the
        ConflictResolver, and on success writes the merged content to the
        working tree + ``git add`` the file.
        """
        base = await self._git(
            ["show", f":1:{fpath}"], cwd=self._project_path,
        )
        ours = await self._git(
            ["show", f":2:{fpath}"], cwd=self._project_path,
        )
        theirs = await self._git(
            ["show", f":3:{fpath}"], cwd=self._project_path,
        )

        # If any stage is missing (e.g. add/add conflict with no base),
        # use empty string as the base — ConflictResolver handles it.
        base_text = base["stdout"] if base["exit_code"] == 0 else ""
        ours_text = ours["stdout"] if ours["exit_code"] == 0 else ""
        theirs_text = theirs["stdout"] if theirs["exit_code"] == 0 else ""

        merge_result = self._resolver.resolve(
            base_text, ours_text, theirs_text,
        )

        # Escalate to LLM-assisted if auto-merge failed and a client exists.
        if not merge_result.success:
            merge_result = self._resolver.resolve(
                base_text, ours_text, theirs_text,
                tier=ResolutionTier.LLM_ASSISTED,
            )

        if not merge_result.success or merge_result.merged is None:
            logger.warning(
                "MergeArbiter: could not resolve conflict in %s", fpath,
            )
            return False

        # Write the merged content to the working tree.
        full_path = os.path.join(self._project_path, fpath)
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
        except OSError:
            pass  # file is at repo root
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(merge_result.merged)
        except OSError as exc:
            logger.warning(
                "MergeArbiter: failed to write resolved %s: %s", fpath, exc,
            )
            return False

        # Stage the resolved file.
        add_res = await self._git(["add", fpath], cwd=self._project_path)
        if add_res["exit_code"] != 0:
            logger.warning(
                "MergeArbiter: git add %s failed: %s",
                fpath, add_res["stderr"][:200],
            )
            return False

        logger.info("MergeArbiter: resolved conflict in %s", fpath)
        return True

    async def _git(self, args: list[str], cwd: str = "") -> dict[str, Any]:
        """Run a git command and return stdout/stderr/exit_code.

        # ponytail: same pattern as WorkspaceManager._git — a shared helper
        # would be cleaner but the pattern is tiny and duplicating it keeps
        # MergeArbiter decoupled from WorkspaceManager (different lifecycle).
        """
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
