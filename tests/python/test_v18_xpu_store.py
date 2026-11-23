"""Tests for the V18 SetupX experience store (#69)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.xpu_store import (
    SetupEvidence,
    SetupXError,
    SetupXRunner,
    SnapshotStack,
    Verdict,
    Xpu,
    XpuStore,
    xpu_store_enabled,
)


def store() -> XpuStore:
    store = XpuStore()
    store.register(Xpu("xpu-a", "missing-libfoo", "install libfoo", ("apt-get install libfoo",)))
    store.register(Xpu("xpu-b", "missing-libfoo", "build libfoo", ("make libfoo",)))
    return store


def test_flag_defaults_off_and_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not xpu_store_enabled()
    monkeypatch.setenv("UC_XPU_STORE", "on")
    assert xpu_store_enabled()


def test_register_and_posterior() -> None:
    current = store()
    assert len(current) == 2
    assert current.posterior("xpu-a") == 0.5
    assert current.posterior("missing") is None


def test_duplicate_and_capacity_are_rejected() -> None:
    current = store()
    with pytest.raises(SetupXError, match="already registered"):
        current.register(Xpu("xpu-a", "s", "g"))
    limited = XpuStore(max_xpus=1)
    limited.register(Xpu("a", "s", "g"))
    with pytest.raises(SetupXError, match="capacity"):
        limited.register(Xpu("b", "s", "g"))


def test_match_prefers_posterior_and_breaks_ties_by_id() -> None:
    current = store()
    assert current.match("missing-libfoo").id == "xpu-a"
    current.record("xpu-b", True)
    assert current.match("missing-libfoo").id == "xpu-b"
    assert current.match("other") is None


def test_speculation_pushes_snapshot() -> None:
    runner = SetupXRunner(store())
    xpu, snapshot = runner.speculate("missing-libfoo")
    assert xpu.id == "xpu-a"
    assert snapshot.depth == 1
    assert len(runner.stack) == 1
    assert runner.pending == snapshot


def test_accept_keeps_snapshot_and_raises_posterior() -> None:
    runner = SetupXRunner(store())
    runner.speculate("missing-libfoo")
    outcome = runner.judge(SetupEvidence(True, True, True))
    assert outcome.verdict is Verdict.ACCEPTED
    assert len(runner.stack) == 1
    assert runner.pending is None
    assert outcome.posterior > 0.5


def test_reject_pops_snapshot_and_lowers_posterior() -> None:
    runner = SetupXRunner(store())
    runner.speculate("missing-libfoo")
    outcome = runner.judge(SetupEvidence(True, False, True))
    assert outcome.verdict is Verdict.REJECTED
    assert len(runner.stack) == 0
    assert outcome.posterior < 0.5


def test_unknown_and_pending_rules() -> None:
    runner = SetupXRunner(store())
    with pytest.raises(SetupXError, match="no xpU"):
        runner.speculate("missing-other")
    with pytest.raises(SetupXError, match="no speculation"):
        runner.judge(SetupEvidence(True, True, True))
    runner.speculate("missing-libfoo")
    with pytest.raises(SetupXError, match="already pending"):
        runner.speculate("missing-libfoo")
    with pytest.raises(SetupXError, match="pending"):
        runner.rollback()


def test_snapshot_capacity_is_enforced() -> None:
    runner = SetupXRunner(store(), SnapshotStack(max_depth=1))
    runner.speculate("missing-libfoo")
    runner.judge(SetupEvidence(True, True, True))
    with pytest.raises(SetupXError, match="snapshot capacity"):
        runner.speculate("missing-libfoo")


def test_rollback_after_accepted_checkpoint() -> None:
    runner = SetupXRunner(store())
    runner.speculate("missing-libfoo")
    runner.judge(SetupEvidence(True, True, True))
    snapshot = runner.rollback()
    assert snapshot.depth == 1
    assert len(runner.stack) == 0
    with pytest.raises(SetupXError, match="empty"):
        runner.rollback()
