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
as-is for its own callers. ``DistributedConflictDetector`` is demoted
to an advisory scheduling hint (in-process only, never a distributed
lock).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ultimate_coders.agent.conflict import ConflictResolver, ResolutionTier

logger = logging.getLogger(__name__)

#: Substrings that mark a push failure as *transport-level*: the request may
#: or may not have reached the remote, so the outcome is UNKNOWN, not
#: failed. A rejection (non-fast-forward, hook declined, auth) is definite
#: and deliberately absent from this list.
_AMBIGUOUS_PUSH_MARKERS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "unable to access",
    "early eof",
    "rpc failed",
    "tls",
    "ssl",
    "broken pipe",
)


def _ambiguous_push_failure(stderr: str) -> bool:
    """True when a non-zero push exit may still have applied the change."""
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in _AMBIGUOUS_PUSH_MARKERS)


# Repo-level git identity used when no global user config exists (e.g. CI
# runners, fresh worker containers). ``git merge --no-edit`` creates a merge
# commit which hard-fails without an identity on Linux — set a local
# (repo-scoped, never global) identity so merges work out of the box.
_ARBITER_IDENTITY_EMAIL = "uc-merge-arbiter@local"
_ARBITER_IDENTITY_NAME = "UC Merge Arbiter"


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
        ledger: Any = None,
    ) -> None:
        self._project_path = project_path or os.getcwd()
        self._remote_url = remote_url
        self._base_branch = base_branch
        self._remote_name = remote_name
        # ConflictResolver is the escalation path for text-level conflicts
        # that git's own merge cannot auto-resolve.
        self._resolver = ConflictResolver(llm_client)
        # Optional effect ledger (effect_ledger.EffectLedger). When present,
        # the publish of the base branch is recorded as one durable effect:
        # a dropped connection becomes UNKNOWN and is reconciled against the
        # remote before any retry, instead of being reported as a failure.
        self._ledger = ledger
        # Serialize concurrent arbitrate() calls on the same clone: two
        # tasks finishing at once would interleave checkout/merge/push on a
        # single working tree and corrupt state. ponytail: per-instance lock,
        # not cross-process — relies on single arbiter process per clone.
        self._arb_lock = asyncio.Lock()

    async def ensure_clone(self) -> None:
        """Ensure a git checkout of the remote exists at ``project_path``.

        Same pattern as ``WorkspaceManager.ensure_clone``: clone if the path
        is not yet a git repo, otherwise ensure the remote URL is correct.
        Idempotent. Raises ``RuntimeError`` on clone failure.

        A repo-level git identity (``user.email`` / ``user.name``) is set on
        the local clone so ``git merge --no-edit`` can create merge commits
        without relying on a global git config (CI runners and worker
        containers typically have none). The config is repo-scoped and
        overwrites any stale value, so it never touches the host's global
        config. Failures are non-fatal (logged at debug).
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
                    self._remote_url,
                    result["stderr"][:300],
                )
                raise RuntimeError(f"git clone failed: {result['stderr'][:200]}")
            logger.info(
                "MergeArbiter.ensure_clone: cloned %s into %s",
                self._remote_url,
                self._project_path,
            )
            await self._ensure_local_identity()
            return

        # Repo exists — ensure origin points at the configured remote.
        cur = await self._git(["remote", "get-url", self._remote_name])
        if cur["exit_code"] != 0 or cur["stdout"].strip() != self._remote_url:
            if cur["exit_code"] != 0:
                add = await self._git(["remote", "add", self._remote_name, self._remote_url])
                if add["exit_code"] != 0:
                    logger.warning(
                        "MergeArbiter.ensure_clone: add remote failed: %s",
                        add["stderr"][:200],
                    )
            else:
                await self._git(["remote", "set-url", self._remote_name, self._remote_url])
            logger.info(
                "MergeArbiter.ensure_clone: remote %s set to %s",
                self._remote_name,
                self._remote_url,
            )

        # (Re)assert the repo-level identity — idempotent. A pre-existing
        # clone (e.g. from a prior run) may still lack one.
        await self._ensure_local_identity()

    async def _ensure_local_identity(self) -> None:
        """Set a repo-level git identity so merge commits can be authored.

        Uses ``git config`` (repo-scoped by default when run inside the repo)
        so the host's global config is never touched. Overwrites any prior
        value, which is safe — the arbiter owns this clone. Non-fatal: a
        failure is logged at debug and the merge will simply surface the real
        error if identity was genuinely unavailable.
        """
        for key, value in (
            ("user.email", _ARBITER_IDENTITY_EMAIL),
            ("user.name", _ARBITER_IDENTITY_NAME),
        ):
            res = await self._git(
                ["config", key, value],
                cwd=self._project_path,
            )
            if res["exit_code"] != 0:
                logger.debug(
                    "MergeArbiter: git config %s failed (non-fatal): %s",
                    key,
                    res["stderr"][:200],
                )

    async def arbitrate(self, subtask_branches: list[str]) -> dict[str, Any]:
        """Merge subtask branches into ``origin/<base_branch>>`` and push.

        Serializes concurrent calls via ``_arb_lock`` — two arbitrate() calls
        on the same clone would interleave git ops on one working tree.
        """
        async with self._arb_lock:
            return await self._arbitrate_impl(subtask_branches)

    async def _arbitrate_impl(self, subtask_branches: list[str]) -> dict[str, Any]:
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
                    "checkout",
                    "-b",
                    self._base_branch,
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
            # Fetch the branch ref from origin. Force-fetch (+refspec) so a
            # stale local ref of the same name is updated, not rejected.
            fetch_res = await self._git(
                [
                    "fetch",
                    self._remote_name,
                    f"+refs/heads/{branch}:refs/heads/{branch}",
                ],
                cwd=self._project_path,
            )
            if fetch_res["exit_code"] != 0:
                # Branch not on remote (worker never pushed / push failed).
                # Record as conflict-distinguishable missing, not a silent
                # merge of a stale local ref.
                conflicts.append(branch)
                logger.warning(
                    "MergeArbiter: fetch of %s failed (missing on remote?): %s",
                    branch,
                    fetch_res["stderr"][:200],
                )
                continue

            merge_res = await self._git(
                ["merge", branch, "--no-edit"],
                cwd=self._project_path,
            )
            if merge_res["exit_code"] == 0:
                merged.append(branch)
                logger.info("MergeArbiter: merged %s", branch)
                continue

            # Non-zero merge. Distinguish real conflict (unmerged files
            # present) from infra errors (bad object, lock, identity) so we
            # don't escalate infra failures to ConflictResolver.
            unmerged_res = await self._git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=self._project_path,
            )
            has_unmerged = unmerged_res["exit_code"] == 0 and bool(unmerged_res["stdout"].strip())
            if not has_unmerged:
                # Infra error, not a conflict. Abort any partial merge
                # state and record; do NOT call ConflictResolver.
                abort_res = await self._git(["merge", "--abort"], cwd=self._project_path)
                if abort_res["exit_code"] != 0:
                    # abort failed — hard reset so the next branch's merge
                    # doesn't hit "merge in progress" (mirrors conflict path).
                    await self._git(
                        ["reset", "--hard", f"{self._remote_name}/{self._base_branch}"],
                        cwd=self._project_path,
                    )
                conflicts.append(branch)
                logger.error(
                    "MergeArbiter: merge of %s failed (non-conflict): %s",
                    branch,
                    merge_res["stderr"][:200],
                )
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
            abort_res = await self._git(["merge", "--abort"], cwd=self._project_path)
            if abort_res["exit_code"] != 0:
                # abort failed — hard reset to clean state so the next
                # branch's merge doesn't hit "merge in progress".
                logger.warning(
                    "MergeArbiter: merge --abort failed, hard resetting: %s",
                    abort_res["stderr"][:200],
                )
                await self._git(
                    ["reset", "--hard", f"{self._remote_name}/{self._base_branch}"],
                    cwd=self._project_path,
                )
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

        # All merged — push main. Plain push (NOT force): the arbiter is the
        # only writer of main, so a non-ff rejection means a concurrent
        # arbiter/external commit advanced main between our fetch and push.
        # --force-with-lease would use our stale remote-tracking ref as the
        # lease and silently overwrite that concurrent work. Instead reject
        # safely; the caller re-runs arbitrate (re-fetch + re-merge is
        # idempotent — subtask branches persist on the remote).
        return await self._push_base_branch(result)

    async def _push_base_branch(self, result: dict[str, Any]) -> dict[str, Any]:
        """Publish the merged base branch, optionally through the ledger.

        Without a ledger the behaviour is the historical one (exit code ->
        pushed/failed). With a ledger the publish is one durable effect
        keyed by (remote, branch, commit being published):

        1. ``prepare`` records the intent; a CONFIRMED record means this
           exact publish already happened -- return ``already_pushed``
           without touching the remote.
        2. An UNKNOWN/RECONCILING record is settled first: applied ->
           ``already_pushed``, definitely absent -> safe to retry,
           inconclusive -> stop and report ``unknown``.
        3. A transport-level push failure becomes UNKNOWN and is
           reconciled with ``ls-remote`` before any retry; only a definite
           rejection is reported as ``failed``.
        """
        effect_id = await self._push_effect_id()
        if self._ledger is not None and effect_id is None:
            # Cannot name the effect (rev-parse failed) -- skip the ledger
            # rather than record under a guessed identity.
            self._ledger = None

        if self._ledger is not None:
            from ultimate_coders.effect_ledger import EffectBusyError

            try:
                record = self._ledger.prepare(
                    effect_id,
                    kind="push-main",
                    request={"branch": self._base_branch, "remote": self._remote_name},
                    expected_after=await self._head(),
                )
            except EffectBusyError as exc:
                result["status"] = "failed"
                result["push_status"] = "busy"
                result["effect_id"] = effect_id
                result["error"] = str(exc)
                logger.error("MergeArbiter: %s", exc)
                return result
            status = record.get("status")
            if status == "CONFIRMED":
                result["status"] = "merged"
                result["push_status"] = "already_pushed"
                result["effect_id"] = effect_id
                return result
            if status in ("UNKNOWN", "RECONCILING"):
                verdict = await self._reconcile_push(effect_id)
                if verdict == "applied":
                    result["status"] = "merged"
                    result["push_status"] = "already_pushed"
                    result["effect_id"] = effect_id
                    return result
                if verdict == "inconclusive":
                    result["status"] = "failed"
                    result["push_status"] = "unknown"
                    result["effect_id"] = effect_id
                    result["error"] = "push outcome unknown; awaiting reconciliation"
                    return result
                # verdict == "absent": the retry below is safe.
            self._ledger.mark_in_flight(effect_id, owner="merge-arbiter")

        push_res = await self._git(
            ["push", self._remote_name, self._base_branch],
            cwd=self._project_path,
        )
        if push_res["exit_code"] == 0:
            result["status"] = "merged"
            result["push_status"] = "pushed"
            if self._ledger is not None:
                self._ledger.confirm(effect_id, result={"branch": self._base_branch})
                result["effect_id"] = effect_id
            logger.info("MergeArbiter: pushed %s", self._base_branch)
            return result

        stderr = push_res["stderr"][:200]
        result["status"] = "failed"
        result["error"] = f"push failed: {stderr}"
        if self._ledger is None:
            result["push_status"] = "failed"
            logger.error("MergeArbiter: %s", result["error"])
            return result

        result["effect_id"] = effect_id
        if not _ambiguous_push_failure(push_res["stderr"]):
            self._ledger.fail(effect_id, error=stderr)
            result["push_status"] = "failed"
            logger.error("MergeArbiter: %s", result["error"])
            return result

        # Transport failure: the remote may already have the commit.
        self._ledger.mark_unknown(effect_id, reason=stderr)
        verdict = await self._reconcile_push(effect_id)
        if verdict == "applied":
            result["status"] = "merged"
            result["push_status"] = "already_pushed"
            result["error"] = ""
            logger.info(
                "MergeArbiter: push reported an error but %s already has the commit",
                self._base_branch,
            )
            return result
        if verdict == "absent":
            result["push_status"] = "failed"
            logger.error("MergeArbiter: %s (reconciled: not applied)", result["error"])
            return result
        result["push_status"] = "unknown"
        logger.error(
            "MergeArbiter: %s (outcome unknown; needs reconciliation)", result["error"]
        )
        return result

    # ── push effect helpers ─────────────────────────────────────

    async def _head(self) -> str:
        head = await self._git(["rev-parse", "HEAD"], cwd=self._project_path)
        return head["stdout"].strip() if head["exit_code"] == 0 else ""

    async def _push_effect_id(self) -> str | None:
        """Stable id for publishing the current HEAD to the base branch."""
        if self._ledger is None:
            return None
        from ultimate_coders.effect_ledger import stable_effect_id

        head = await self._head()
        if not head:
            return None
        return stable_effect_id("push-main", self._remote_name, self._base_branch, head)

    async def _reconcile_push(self, effect_id: str) -> str:
        """Ask the remote whether the publish landed.

        Returns ``"applied"``, ``"absent"`` or ``"inconclusive"``. Only
        ``absent`` authorises a retry; ``inconclusive`` leaves the effect
        UNKNOWN for an operator or a later pass.
        """
        record = self._ledger.get(effect_id) or {}
        if record.get("status") == "PREPARED":
            self._ledger.mark_in_flight(effect_id, owner="merge-arbiter")
        if record.get("status") != "RECONCILING":
            self._ledger.begin_reconcile(effect_id, owner="merge-arbiter")

        expected_after = await self._head()
        before = await self._git(
            ["rev-parse", f"{self._remote_name}/{self._base_branch}"],
            cwd=self._project_path,
        )
        expected_before = before["stdout"].strip() if before["exit_code"] == 0 else ""

        remote = await self._git(
            ["ls-remote", self._remote_name, f"refs/heads/{self._base_branch}"],
            cwd=self._project_path,
        )
        if remote["exit_code"] != 0:
            # The reconciler could not ask either: hand the effect back as
            # UNKNOWN so a later pass (or an operator) picks it up instead
            # of leaving a RECONCILING record nobody owns.
            self._ledger.mark_unknown(effect_id, reason="reconcile could not reach the remote")
            return "inconclusive"
        parts = remote["stdout"].split()
        current = parts[0] if parts else ""

        if expected_after and current == expected_after:
            self._ledger.confirm(
                effect_id, result={"branch": self._base_branch, "head": current}
            )
            return "applied"
        if current in (expected_before, ""):
            self._ledger.resolve_reconcile(
                effect_id, applied=False, reason="remote does not have the commit"
            )
            return "absent"
        self._ledger.mark_unknown(effect_id, reason="remote moved to an unexpected head")
        return "inconclusive"

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

        files = [f for f in files_res["stdout"].splitlines() if f.strip()]
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
            ["show", f":1:{fpath}"],
            cwd=self._project_path,
        )
        ours = await self._git(
            ["show", f":2:{fpath}"],
            cwd=self._project_path,
        )
        theirs = await self._git(
            ["show", f":3:{fpath}"],
            cwd=self._project_path,
        )

        # If any stage is missing (e.g. add/add conflict with no base),
        # use empty string as the base — ConflictResolver handles it.
        base_text = base["stdout"] if base["exit_code"] == 0 else ""
        ours_text = ours["stdout"] if ours["exit_code"] == 0 else ""
        theirs_text = theirs["stdout"] if theirs["exit_code"] == 0 else ""

        merge_result = self._resolver.resolve(
            base_text,
            ours_text,
            theirs_text,
        )

        # Escalate to LLM-assisted if auto-merge failed and a client exists.
        if not merge_result.success:
            merge_result = self._resolver.resolve(
                base_text,
                ours_text,
                theirs_text,
                tier=ResolutionTier.LLM_ASSISTED,
            )

        if not merge_result.success or merge_result.merged is None:
            logger.warning(
                "MergeArbiter: could not resolve conflict in %s",
                fpath,
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
                "MergeArbiter: failed to write resolved %s: %s",
                fpath,
                exc,
            )
            return False

        # Stage the resolved file.
        add_res = await self._git(["add", fpath], cwd=self._project_path)
        if add_res["exit_code"] != 0:
            logger.warning(
                "MergeArbiter: git add %s failed: %s",
                fpath,
                add_res["stderr"][:200],
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
            "git",
            *args,
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
