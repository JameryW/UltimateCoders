"""Subtask-branch publish through the effect ledger.

Mirrors the merge-arbiter table for the worker-side push: a rejection is
definite, a transport failure is UNKNOWN until ``ls-remote`` says
otherwise, and a CONFIRMED record means the publish already happened.
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.workspace import WorkspaceHandle, WorkspaceManager
from ultimate_coders.effect_ledger import (
    EffectLedger,
    EffectStatus,
    stable_effect_id,
)
from ultimate_coders.runtime_state import RuntimeState

HEAD = "c" * 40
OLD = "d" * 40
BRANCH = "uc/subtask/abc123"


class FakeGit:
    def __init__(
        self,
        *,
        push_exit: int = 0,
        push_stderr: str = "",
        remote_head: str = HEAD,
        ls_remote_exit: int = 0,
    ) -> None:
        self.push_exit = push_exit
        self.push_stderr = push_stderr
        self.remote_head = remote_head
        self.ls_remote_exit = ls_remote_exit
        self.calls: list[list[str]] = []

    async def __call__(self, args, cwd=""):
        self.calls.append(list(args))
        head = args[0] if args else ""
        if head == "push":
            return {"exit_code": self.push_exit, "stdout": "", "stderr": self.push_stderr}
        if head == "ls-remote":
            out = f"{self.remote_head}\trefs/heads/{BRANCH}\n" if self.remote_head else ""
            return {"exit_code": self.ls_remote_exit, "stdout": out, "stderr": ""}
        if args[:1] == ["rev-parse"] and args[1] == BRANCH:
            return {"exit_code": 0, "stdout": HEAD + "\n", "stderr": ""}
        if args[:1] == ["rev-parse"]:
            return {"exit_code": 0, "stdout": OLD + "\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def pushed(self) -> bool:
        return any(c[:1] == ["push"] for c in self.calls)


def _manager(tmp_path, *, with_state: bool = True):
    mgr = WorkspaceManager(
        project_path=str(tmp_path),
        remote_url="https://example.invalid/repo.git",
        push_on_release=True,
    )
    if with_state:
        mgr.bind_runtime_state(RuntimeState(tmp_path / "state.db"))
    return mgr


def _handle() -> WorkspaceHandle:
    return WorkspaceHandle(
        workspace_id="ws-1",
        subtask_id="st-1",
        worktree_path="/tmp/wt",
        branch_name=BRANCH,
    )


def _ledger(mgr) -> EffectLedger:
    return EffectLedger(mgr._state)


@pytest.mark.asyncio
async def test_success_is_pushed_and_confirmed(tmp_path):
    mgr = _manager(tmp_path)
    git = FakeGit()
    mgr._git = git  # type: ignore[assignment]
    info: dict = {"status": "merged"}
    await mgr._push_subtask_branch(_handle(), info)
    assert info["push_status"] == "pushed"
    record = _ledger(mgr).get(info["effect_id"])
    assert record["status"] == EffectStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_rejection_is_definite_failure(tmp_path):
    mgr = _manager(tmp_path)
    git = FakeGit(push_exit=1, push_stderr="! [rejected] ... (non-fast-forward)")
    mgr._git = git  # type: ignore[assignment]
    info: dict = {"status": "merged"}
    await mgr._push_subtask_branch(_handle(), info)
    assert info["push_status"] == "failed"
    assert _ledger(mgr).get(info["effect_id"])["status"] == EffectStatus.FAILED.value
    assert not any(c[:1] == ["ls-remote"] for c in git.calls)


@pytest.mark.asyncio
async def test_lost_response_reconciles_as_already_pushed(tmp_path):
    mgr = _manager(tmp_path)
    git = FakeGit(
        push_exit=128,
        push_stderr="fatal: unable to access ... Connection timed out",
        remote_head=HEAD,
    )
    mgr._git = git  # type: ignore[assignment]
    info: dict = {"status": "merged"}
    await mgr._push_subtask_branch(_handle(), info)
    assert info["push_status"] == "already_pushed"
    assert _ledger(mgr).get(info["effect_id"])["status"] == EffectStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_transport_failure_without_remote_commit_allows_retry(tmp_path):
    mgr = _manager(tmp_path)
    git = FakeGit(
        push_exit=128,
        push_stderr="fatal: unable to access ... Could not resolve host",
        remote_head="",
    )
    mgr._git = git  # type: ignore[assignment]
    info: dict = {"status": "merged"}
    await mgr._push_subtask_branch(_handle(), info)
    assert info["push_status"] == "failed"
    assert _ledger(mgr).get(info["effect_id"])["status"] == EffectStatus.PREPARED.value


@pytest.mark.asyncio
async def test_inconclusive_reconcile_is_unknown(tmp_path):
    mgr = _manager(tmp_path)
    git = FakeGit(
        push_exit=128,
        push_stderr="fatal: unable to access ... Connection reset by peer",
        ls_remote_exit=128,
    )
    mgr._git = git  # type: ignore[assignment]
    info: dict = {"status": "merged"}
    await mgr._push_subtask_branch(_handle(), info)
    assert info["push_status"] == "unknown"
    assert _ledger(mgr).get(info["effect_id"])["status"] == EffectStatus.UNKNOWN.value


@pytest.mark.asyncio
async def test_confirmed_effect_skips_the_remote(tmp_path):
    mgr = _manager(tmp_path)
    effect_id = stable_effect_id("push-branch", "origin", BRANCH, HEAD)
    ledger = _ledger(mgr)
    ledger.prepare(effect_id, kind="push-branch")
    ledger.mark_in_flight(effect_id, owner="earlier")
    ledger.confirm(effect_id)

    git = FakeGit()
    mgr._git = git  # type: ignore[assignment]
    info: dict = {"status": "merged"}
    await mgr._push_subtask_branch(_handle(), info)
    assert info["push_status"] == "already_pushed"
    assert not git.pushed()


@pytest.mark.asyncio
async def test_no_bound_state_keeps_legacy_behaviour(tmp_path):
    mgr = _manager(tmp_path, with_state=False)
    git = FakeGit(push_exit=128, push_stderr="fatal: unable to access ... timeout")
    mgr._git = git  # type: ignore[assignment]
    info: dict = {"status": "merged"}
    await mgr._push_subtask_branch(_handle(), info)
    assert info["push_status"] == "failed"
    assert "effect_id" not in info
