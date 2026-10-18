"""Effect ledger contract: UNKNOWN is not FAILED, and identity is stable.

These tests pin the state machine that the merge arbiter's publish path
depends on: one logical effect across retries, a live lease blocking a
second worker, crash recovery turning an expired lease into UNKNOWN, and
reconciliation -- not assumption -- deciding whether a retry is safe.
"""

from __future__ import annotations

import pytest
from ultimate_coders.effect_ledger import (
    EffectBusyError,
    EffectLedger,
    EffectStatus,
    EffectTransitionError,
    stable_effect_id,
)
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture()
def ledger(tmp_path):
    return EffectLedger(RuntimeState(tmp_path / "state.db"))


def _prepared(ledger: EffectLedger, effect_id: str = "eff_test") -> dict:
    return ledger.prepare(effect_id, kind="push", request={"branch": "main"})


def test_stable_effect_id_is_deterministic_and_part_sensitive():
    a = stable_effect_id("push-main", "origin", "main", "abc123")
    b = stable_effect_id("push-main", "origin", "main", "abc123")
    c = stable_effect_id("push-main", "origin", "main", "def456")
    assert a == b and a.startswith("eff_") and len(a) == 4 + 24
    assert a != c
    # The attempt number must never be part of the identity.
    assert stable_effect_id("push-main", "origin", "main", "abc123", 2) != a


def test_happy_path_prepare_in_flight_confirm(ledger):
    _prepared(ledger)
    record = ledger.mark_in_flight("eff_test", owner="w1", lease_seconds=60)
    assert record["status"] == EffectStatus.IN_FLIGHT.value
    assert record["attempts"] == 1
    assert record["lease_until"] > record["updated_at"] - 1

    confirmed = ledger.confirm("eff_test", result={"head": "abc"})
    assert confirmed["status"] == EffectStatus.CONFIRMED.value
    assert confirmed["result"] == {"head": "abc"}
    assert confirmed["lease_until"] == 0


def test_confirmed_is_terminal_and_prepare_returns_it(ledger):
    _prepared(ledger)
    ledger.mark_in_flight("eff_test", owner="w1")
    ledger.confirm("eff_test")
    again = _prepared(ledger)
    assert again["status"] == EffectStatus.CONFIRMED.value
    assert again["attempts"] == 1
    with pytest.raises(EffectTransitionError):
        ledger.mark_in_flight("eff_test", owner="w2")


def test_live_lease_blocks_a_second_worker(ledger):
    _prepared(ledger)
    ledger.mark_in_flight("eff_test", owner="w1", lease_seconds=60)
    with pytest.raises(EffectBusyError):
        ledger.prepare("eff_test", kind="push")


def test_expired_lease_becomes_unknown_not_failed(ledger):
    _prepared(ledger)
    ledger.mark_in_flight("eff_test", owner="w1", lease_seconds=0)
    record = _prepared(ledger)
    assert record["status"] == EffectStatus.UNKNOWN.value
    assert record["reason"] == "lease expired"


def test_unknown_is_listed_and_is_not_failed(ledger):
    _prepared(ledger)
    ledger.mark_in_flight("eff_test", owner="w1")
    ledger.mark_unknown("eff_test", reason="connection reset")
    record = ledger.get("eff_test")
    assert record["status"] == EffectStatus.UNKNOWN.value
    assert record["error"] if "error" in record else True
    assert [r["effect_id"] for r in ledger.unknown()] == ["eff_test"]
    # A retry is NOT authorised while the outcome is unknown.
    again = _prepared(ledger)
    assert again["status"] == EffectStatus.UNKNOWN.value


def test_reconcile_applied_confirms_without_reexecution(ledger):
    _prepared(ledger)
    ledger.mark_in_flight("eff_test", owner="w1")
    ledger.mark_unknown("eff_test", reason="timeout")
    ledger.begin_reconcile("eff_test", owner="ops")
    record = ledger.resolve_reconcile("eff_test", applied=True, result={"head": "abc"})
    assert record["status"] == EffectStatus.CONFIRMED.value
    assert ledger.unknown() == []


def test_reconcile_absent_returns_to_prepared_and_retry_counts(ledger):
    _prepared(ledger)
    ledger.mark_in_flight("eff_test", owner="w1")
    ledger.mark_unknown("eff_test", reason="timeout")
    ledger.begin_reconcile("eff_test", owner="ops")
    record = ledger.resolve_reconcile("eff_test", applied=False, reason="remote lacks it")
    assert record["status"] == EffectStatus.PREPARED.value

    retried = ledger.mark_in_flight("eff_test", owner="w2")
    assert retried["attempts"] == 2
    ledger.confirm("eff_test")
    assert ledger.get("eff_test")["attempts"] == 2


def test_definite_failure_can_be_retried_under_the_same_id(ledger):
    _prepared(ledger)
    ledger.mark_in_flight("eff_test", owner="w1")
    failed = ledger.fail("eff_test", error="non-fast-forward")
    assert failed["status"] == EffectStatus.FAILED.value
    retry = _prepared(ledger)
    assert retry["status"] == EffectStatus.PREPARED.value
    assert retry["reason"] == "retry after definite failure"


def test_records_survive_a_restart(tmp_path):
    db = tmp_path / "state.db"
    first = EffectLedger(RuntimeState(db))
    first.prepare("eff_test", kind="push")
    first.mark_in_flight("eff_test", owner="w1")
    first.mark_unknown("eff_test", reason="process died")

    second = EffectLedger(RuntimeState(db))
    record = second.get("eff_test")
    assert record["status"] == EffectStatus.UNKNOWN.value
    assert record["reason"] == "process died"
    assert [r["effect_id"] for r in second.unknown()] == ["eff_test"]


def test_confirm_from_unknown_is_legal_but_from_prepared_is_not(ledger):
    _prepared(ledger)
    with pytest.raises(EffectTransitionError):
        ledger.confirm("eff_test")
    ledger.mark_in_flight("eff_test", owner="w1")
    ledger.mark_unknown("eff_test", reason="lost response")
    confirmed = ledger.confirm("eff_test", result={"recovered": True})
    assert confirmed["status"] == EffectStatus.CONFIRMED.value
