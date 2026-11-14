"""git calls in the workspace manager must be bounded, and a timed-out push reconciled (T66).

`WorkspaceManager._git` awaited `proc.communicate()` with no timeout, and it drives acquire,
release and the branch push. A hung fetch/push (DNS blackhole, credential prompt, dead SSH)
therefore blocked the caller while the worktree stayed held — the unbounded-call class T50
fixed for `subprocess.run`, in the async-subprocess form this module uses.

A timed-out *push* is the interesting case: the remote may have applied it, so it must be
reconciled (UNKNOWN) rather than recorded as a definite failure — otherwise the ledger
authorises a retry of a publish that already landed.
"""

from __future__ import annotations

import asyncio

import pytest
from ultimate_coders.agent import workspace as workspace_module
from ultimate_coders.agent.workspace import (
    WorkspaceHandle,
    WorkspaceManager,
    subtask_branch,
)
from ultimate_coders.effect_ledger import EffectLedger, EffectStatus
from ultimate_coders.runtime_state import RuntimeState

HEAD = "a" * 40
BRANCH = subtask_branch("subtask-timeout-1")
TIMEOUT_STDERR = "git push timed out after 120s"


class _HungProc:
    """A process whose communicate() never returns until it is killed."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(3600)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


async def test_a_hung_git_call_is_bounded_and_killed(tmp_path, monkeypatch):
    manager = WorkspaceManager(project_path=str(tmp_path))
    hung = _HungProc()

    async def fake_exec(*args, **kwargs):
        return hung

    monkeypatch.setattr(workspace_module, "GIT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await manager._git(["fetch", "origin"])

    assert result["exit_code"] == -1
    assert result["timed_out"] is True
    assert "timed out after 0.2s" in result["stderr"]
    assert hung.killed is True, "a hung git must be killed, not abandoned"


@pytest.fixture()
def state(tmp_path) -> RuntimeState:
    return RuntimeState(tmp_path / "state.db")


async def test_a_timed_out_push_is_reconciled_not_failed(tmp_path, state):
    manager = WorkspaceManager(
        project_path=str(tmp_path),
        remote_url="https://example.invalid/repo.git",
        push_on_release=True,
    )
    manager._state = state
    handle = WorkspaceHandle(
        workspace_id="ws-1",
        branch_name=BRANCH,
        subtask_id="subtask-timeout-1",
        project_path=str(tmp_path),
    )

    async def fake_git(args, cwd=""):
        if args[:1] == ["rev-parse"]:
            return {"exit_code": 0, "stdout": HEAD + "\n", "stderr": "", "timed_out": False}
        if args[:1] == ["push"]:
            return {"exit_code": -1, "stdout": "", "stderr": TIMEOUT_STDERR, "timed_out": True}
        # ls-remote during reconcile: the remote cannot be reached either.
        return {"exit_code": 128, "stdout": "", "stderr": "unreachable", "timed_out": False}

    manager._git = fake_git  # type: ignore[assignment]
    result: dict = {}

    await manager._push_subtask_branch(handle, result)

    assert result["push_status"] == "unknown", "a timed-out push is not a definite failure"
    record = EffectLedger(state).get(result["effect_id"])
    assert record["status"] == EffectStatus.UNKNOWN.value
    assert record["status"] != EffectStatus.FAILED.value
