"""Merge arbiter publish semantics with the effect ledger.

A fake git runner keeps these tests hermetic: the point is the decision
table around a push -- definite rejection vs transport failure, reconcile
before retry, and the pre-confirmed case that must not touch the remote
at all.
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.merge_arbiter import MergeArbiter, _ambiguous_push_failure
from ultimate_coders.effect_ledger import (
    EffectLedger,
    EffectStatus,
    stable_effect_id,
)
from ultimate_coders.runtime_state import RuntimeState

NEW_HEAD = "a" * 40
OLD_HEAD = "b" * 40


class FakeGit:
    """Answers the subset of git calls _push_base_branch makes."""

    def __init__(
        self,
        *,
        push_exit: int = 0,
        push_stderr: str = "",
        remote_head: str = NEW_HEAD,
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
            out = f"{self.remote_head}\trefs/heads/main\n" if self.remote_head else ""
            return {"exit_code": self.ls_remote_exit, "stdout": out, "stderr": ""}
        if args[:2] == ["rev-parse", "HEAD"]:
            return {"exit_code": 0, "stdout": NEW_HEAD + "\n", "stderr": ""}
        if args[:1] == ["rev-parse"]:
            return {"exit_code": 0, "stdout": OLD_HEAD + "\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def pushed(self) -> bool:
        return any(c[:1] == ["push"] for c in self.calls)


def _arbiter(tmp_path, ledger, git):
    arbiter = MergeArbiter(
        project_path=str(tmp_path),
        remote_url="https://example.invalid/repo.git",
        base_branch="main",
        ledger=ledger,
    )
    arbiter._git = git  # type: ignore[assignment]
    return arbiter


@pytest.fixture()
def ledger(tmp_path):
    return EffectLedger(RuntimeState(tmp_path / "state.db"))


def test_classifier_separates_transport_from_rejection():
    assert _ambiguous_push_failure("fatal: unable to access ... Connection timed out")
    assert _ambiguous_push_failure("OpenSSL SSL_read: Connection reset by peer")
    assert not _ambiguous_push_failure("! [rejected] main -> main (non-fast-forward)")
    assert not _ambiguous_push_failure("remote: Permission denied")


@pytest.mark.asyncio
async def test_success_confirms_the_effect(tmp_path, ledger):
    git = FakeGit()
    result = await _arbiter(tmp_path, ledger, git)._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "pushed"
    assert result["status"] == "merged"
    record = ledger.get(result["effect_id"])
    assert record["status"] == EffectStatus.CONFIRMED.value
    assert record["attempts"] == 1


@pytest.mark.asyncio
async def test_rejection_is_failed_and_never_reconciled(tmp_path, ledger):
    git = FakeGit(push_exit=1, push_stderr="! [rejected] main -> main (non-fast-forward)")
    result = await _arbiter(tmp_path, ledger, git)._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "failed"
    assert ledger.get(result["effect_id"])["status"] == EffectStatus.FAILED.value
    assert not any(c[:1] == ["ls-remote"] for c in git.calls)


@pytest.mark.asyncio
async def test_lost_response_is_reconciled_as_already_pushed(tmp_path, ledger):
    """The dangerous case: the push landed, the response did not."""
    git = FakeGit(
        push_exit=128,
        push_stderr="fatal: unable to access 'https://...': Connection timed out",
        remote_head=NEW_HEAD,
    )
    result = await _arbiter(tmp_path, ledger, git)._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "already_pushed"
    assert result["status"] == "merged"
    record = ledger.get(result["effect_id"])
    assert record["status"] == EffectStatus.CONFIRMED.value
    assert record["result"]["head"] == NEW_HEAD


@pytest.mark.asyncio
async def test_transport_failure_with_old_remote_head_authorises_retry(tmp_path, ledger):
    git = FakeGit(
        push_exit=128,
        push_stderr="fatal: unable to access 'https://...': Could not resolve host",
        remote_head=OLD_HEAD,
    )
    result = await _arbiter(tmp_path, ledger, git)._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "failed"
    # Definitely not applied -> back to PREPARED, so a retry is safe.
    assert ledger.get(result["effect_id"])["status"] == EffectStatus.PREPARED.value


@pytest.mark.asyncio
async def test_inconclusive_reconcile_leaves_unknown(tmp_path, ledger):
    git = FakeGit(
        push_exit=128,
        push_stderr="fatal: unable to access 'https://...': Connection timed out",
        ls_remote_exit=128,
    )
    result = await _arbiter(tmp_path, ledger, git)._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "unknown"
    record = ledger.get(result["effect_id"])
    assert record["status"] == EffectStatus.UNKNOWN.value
    # And the next prepare must NOT authorise another push.
    again = ledger.prepare(result["effect_id"], kind="push-main")
    assert again["status"] == EffectStatus.UNKNOWN.value


@pytest.mark.asyncio
async def test_confirmed_effect_skips_the_remote_entirely(tmp_path, ledger):
    effect_id = stable_effect_id("push-main", "origin", "main", NEW_HEAD)
    ledger.prepare(effect_id, kind="push-main")
    ledger.mark_in_flight(effect_id, owner="earlier")
    ledger.confirm(effect_id, result={"head": NEW_HEAD})

    git = FakeGit()
    result = await _arbiter(tmp_path, ledger, git)._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "already_pushed"
    assert not git.pushed()


@pytest.mark.asyncio
async def test_live_lease_reports_busy_without_pushing(tmp_path, ledger):
    effect_id = stable_effect_id("push-main", "origin", "main", NEW_HEAD)
    ledger.prepare(effect_id, kind="push-main")
    ledger.mark_in_flight(effect_id, owner="other-arbiter", lease_seconds=60)

    git = FakeGit()
    result = await _arbiter(tmp_path, ledger, git)._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "busy"
    assert not git.pushed()


@pytest.mark.asyncio
async def test_without_a_ledger_behaviour_is_unchanged(tmp_path):
    git = FakeGit()
    arbiter = MergeArbiter(project_path=str(tmp_path), remote_url="x", base_branch="main")
    arbiter._git = git  # type: ignore[assignment]
    result = await arbiter._push_base_branch({"status": "skipped"})
    assert result["push_status"] == "pushed"
    assert "effect_id" not in result

    git2 = FakeGit(push_exit=128, push_stderr="fatal: unable to access ... timeout")
    arbiter._git = git2  # type: ignore[assignment]
    failed = await arbiter._push_base_branch({"status": "skipped"})
    assert failed["push_status"] == "failed"
