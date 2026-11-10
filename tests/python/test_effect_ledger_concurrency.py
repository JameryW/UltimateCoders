"""Concurrency invariants of the effect ledger's reconcile claim (T55).

Every other test of the ledger is single-threaded, so the one property that makes
reconciliation safe was unproven: **at most one owner may hold a claim at a time**.
If two did, both could act on the same external system (a second push, a second
`evolve-kernel` run, a duplicate stop) — the exact class the ledger exists to prevent.

These tests drive real threads against one SQLite store. `RuntimeState.mutate` uses
`BEGIN IMMEDIATE`, so the read-modify-write inside `begin_reconcile` is atomic; the
assertions below are what "that means" for the caller.
"""

from __future__ import annotations

import threading
import time

import pytest
from ultimate_coders.effect_ledger import (
    EffectBusyError,
    EffectLedger,
    EffectStatus,
    stable_effect_id,
)
from ultimate_coders.runtime_state import RuntimeState

THREADS = 8


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime-state"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return EffectLedger(RuntimeState())


def _unknown(ledger: EffectLedger, name: str = "race") -> str:
    effect_id = stable_effect_id("push-branch", name)
    ledger.prepare(effect_id, kind="push-branch", request={"branch": f"uc/subtask/{name}"})
    ledger.mark_in_flight(effect_id, owner="worker-1")
    ledger.mark_unknown(effect_id, reason="lost response")
    return effect_id


def _race(action) -> list[str]:
    """Run ``action(index)`` in THREADS threads released together; return outcomes."""
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def run(index: int) -> None:
        barrier.wait()
        try:
            outcome = action(index) or "won"
        except EffectBusyError:
            outcome = "busy"
        except Exception as exc:  # noqa: BLE001 - the point is to see it, not hide it
            outcome = f"error:{type(exc).__name__}"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_only_one_reconciler_wins_the_claim(ledger):
    effect_id = _unknown(ledger)

    def claim(index: int) -> None:
        EffectLedger(RuntimeState()).begin_reconcile(
            effect_id, owner=f"ops:{index}", lease_seconds=120
        )

    outcomes = _race(claim)
    assert sorted(outcomes) == ["busy"] * (THREADS - 1) + ["won"]

    record = ledger.state.get(ledger.namespace, effect_id)
    assert record["status"] == EffectStatus.RECONCILING.value
    assert record["reconcile_attempts"] == 1, "a refused claim must not count as an attempt"


def test_only_one_takes_over_an_expired_lease(ledger):
    effect_id = _unknown(ledger)
    ledger.begin_reconcile(effect_id, owner="ops:crashed", lease_seconds=0.05)
    time.sleep(0.2)

    def take(index: int) -> None:
        EffectLedger(RuntimeState()).begin_reconcile(
            effect_id, owner=f"ops:{index}", lease_seconds=120
        )

    outcomes = _race(take)
    assert sorted(outcomes) == ["busy"] * (THREADS - 1) + ["won"]

    record = ledger.state.get(ledger.namespace, effect_id)
    assert record["reconcile_attempts"] == 2, "one crash + one takeover"
    assert record["owner"].startswith("ops:")
    assert record["reason"] == "reconcile lease expired; taken over"


def test_a_stale_reconciler_cannot_settle_a_live_claim_under_contention(ledger):
    effect_id = _unknown(ledger)
    ledger.begin_reconcile(effect_id, owner="ops:holder", lease_seconds=120)

    def stale(index: int) -> None:
        EffectLedger(RuntimeState()).resolve_reconcile(
            effect_id, applied=True, owner="ops:crashed"
        )

    assert sorted(_race(stale)) == ["busy"] * THREADS, "the fence must hold every time"

    record = ledger.state.get(ledger.namespace, effect_id)
    assert record["status"] == EffectStatus.RECONCILING.value, "a refused settle changes nothing"

    # The real holder can still settle, and does so exactly once.
    ledger.resolve_reconcile(
        effect_id, applied=True, result={"remote_id": "r-1"}, owner="ops:holder"
    )
    settled = ledger.state.get(ledger.namespace, effect_id)
    assert settled["status"] == EffectStatus.CONFIRMED.value
    assert settled["owner"] == ""
    assert settled["result"] == {"remote_id": "r-1"}
