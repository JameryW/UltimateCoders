"""A branch-push reconcile held by another owner must not fail the release (T57).

`WorkspaceManager._settle_branch_push` used to call `resolve_reconcile` (which is
owner-fenced) and `begin_reconcile` (which raises when a live claim exists) without
handling the refusal. Two workers reconciling the same subtask-branch publish is a normal
distributed outcome, and it must come back as "inconclusive" -- the release continues and
the other owner's claim is untouched -- rather than as an exception mid-release.
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.workspace import WorkspaceManager
from ultimate_coders.effect_ledger import EffectLedger, EffectStatus, stable_effect_id
from ultimate_coders.runtime_state import RuntimeState

BRANCH = "uc/subtask/abc123"
HEAD = "a" * 40
OLD_HEAD = "b" * 40


class FakeGit:
    def __init__(self, *, remote_head: str, ls_remote_exit: int = 0) -> None:
        self.remote_head = remote_head
        self.ls_remote_exit = ls_remote_exit
        self.calls: list[list[str]] = []

    async def __call__(self, args, cwd=""):
        self.calls.append(list(args))
        if args[:1] == ["ls-remote"]:
            out = f"{self.remote_head}\trefs/heads/{BRANCH}\n" if self.remote_head else ""
            return {"exit_code": self.ls_remote_exit, "stdout": out, "stderr": ""}
        if args[:1] == ["rev-parse"]:
            return {"exit_code": 0, "stdout": OLD_HEAD + "\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}


@pytest.fixture()
def ledger(tmp_path) -> EffectLedger:
    return EffectLedger(RuntimeState(tmp_path / "state.db"))


def _manager(tmp_path, git) -> WorkspaceManager:
    manager = WorkspaceManager(
        project_path=str(tmp_path),
        remote_url="https://example.invalid/repo.git",
        push_on_release=True,
    )
    manager._git = git  # type: ignore[assignment]
    return manager


def _held_elsewhere(ledger: EffectLedger) -> str:
    effect_id = stable_effect_id("push-branch", "origin", BRANCH, HEAD)
    ledger.prepare(effect_id, kind="push-branch", request={"branch": BRANCH}, expected_after=HEAD)
    ledger.mark_in_flight(effect_id, owner="worker-1", lease_seconds=60)
    ledger.mark_unknown(effect_id, reason="lost response")
    ledger.begin_reconcile(effect_id, owner="ops:other", lease_seconds=60)
    return effect_id


@pytest.mark.asyncio
async def test_a_verdict_fenced_away_is_inconclusive_not_a_crash(tmp_path, ledger):
    effect_id = _held_elsewhere(ledger)
    git = FakeGit(remote_head=OLD_HEAD)

    verdict = await _manager(tmp_path, git)._settle_branch_push(ledger, effect_id, BRANCH, HEAD)

    assert verdict == "inconclusive"
    record = ledger.get(effect_id)
    assert record["status"] == EffectStatus.RECONCILING.value
    assert record["owner"] == "ops:other", "the other owner's claim is untouched"


@pytest.mark.asyncio
async def test_an_unreachable_remote_leaves_the_effect_unknown(tmp_path, ledger):
    effect_id = stable_effect_id("push-branch", "origin", BRANCH, HEAD)
    ledger.prepare(effect_id, kind="push-branch", request={"branch": BRANCH})
    ledger.mark_in_flight(effect_id, owner="worker-1", lease_seconds=60)
    ledger.mark_unknown(effect_id, reason="lost response")

    git = FakeGit(remote_head=OLD_HEAD, ls_remote_exit=128)
    verdict = await _manager(tmp_path, git)._settle_branch_push(ledger, effect_id, BRANCH, HEAD)

    assert verdict == "inconclusive"
    assert ledger.get(effect_id)["status"] == EffectStatus.UNKNOWN.value


@pytest.mark.asyncio
async def test_the_landed_publish_is_confirmed(tmp_path, ledger):
    effect_id = stable_effect_id("push-branch", "origin", BRANCH, HEAD)
    ledger.prepare(effect_id, kind="push-branch", request={"branch": BRANCH})
    ledger.mark_in_flight(effect_id, owner="worker-1", lease_seconds=60)
    ledger.mark_unknown(effect_id, reason="lost response")

    git = FakeGit(remote_head=HEAD)
    verdict = await _manager(tmp_path, git)._settle_branch_push(ledger, effect_id, BRANCH, HEAD)

    assert verdict == "applied"
    record = ledger.get(effect_id)
    assert record["status"] == EffectStatus.CONFIRMED.value
    assert record["result"]["head"] == HEAD
