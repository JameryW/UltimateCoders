"""Tests for the sync-to-async promotion guard and convergence notices."""

from __future__ import annotations

import pytest
from ultimate_coders.architecture.sync_async_promotion import (
    ConvergenceNotice,
    PendingDecision,
    SyncAsyncPromoter,
    promotion_enabled,
)


class FakeClock:
    """Deterministic clock: tests advance it explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _promoter(
    clock: FakeClock,
    threshold: float = 5.0,
    max_pending: int = 256,
) -> SyncAsyncPromoter:
    return SyncAsyncPromoter(
        threshold_seconds=threshold, now=clock, max_pending=max_pending
    )


def test_submit_tracks_pending_decision() -> None:
    clock = FakeClock(100.0)
    promoter = _promoter(clock)
    decision = promoter.submit("d1", {"kind": "approval"})
    assert decision.decision_id == "d1"
    assert decision.status == "pending"
    assert decision.created_at == 100.0
    assert decision.payload == {"kind": "approval"}
    assert promoter.pending() == (decision,)


def test_submit_is_idempotent_per_decision_id() -> None:
    clock = FakeClock()
    promoter = _promoter(clock)
    first = promoter.submit("d1", {"kind": "first"})
    clock.advance(2.0)
    second = promoter.submit("d1", {"kind": "second"})
    assert second == first
    assert second.payload == {"kind": "first"}
    assert second.created_at == 0.0
    assert len(promoter) == 1


def test_check_promotes_at_threshold_with_fake_now() -> None:
    clock = FakeClock()
    promoter = _promoter(clock)
    promoter.submit("d1", {})
    clock.advance(4.9)
    still_pending = promoter.check("d1")
    assert still_pending.status == "pending"

    clock.advance(0.1)
    promoted = promoter.check("d1")
    assert promoted.status == "promoted"
    assert "threshold" in promoted.reason
    assert promoter.check("d1").status == "promoted"
    assert promoter.pending() == ()

    notices = promoter.notices()
    assert len(notices) == 1
    notice = notices[0]
    assert notice.decision_id == "d1"
    assert notice.status == "promoted"
    assert notice.elapsed_seconds == 5.0


def test_promote_due_only_finishes_expired_decisions() -> None:
    clock = FakeClock()
    promoter = _promoter(clock)
    promoter.submit("old", {})
    clock.advance(3.0)
    promoter.submit("new", {})
    clock.advance(2.0)

    promoted = promoter.promote_due()
    assert [decision.decision_id for decision in promoted] == ["old"]
    assert [decision.decision_id for decision in promoter.pending()] == ["new"]
    assert promoter.promote_due() == ()

    clock.advance(3.0)  # t=8: "new" (created at t=3) has now waited 5s
    assert [decision.decision_id for decision in promoter.promote_due()] == ["new"]
    notices = promoter.notices()
    assert [notice.decision_id for notice in notices] == ["old", "new"]
    assert notices[0].elapsed_seconds == 5.0
    assert notices[1].elapsed_seconds == 5.0


def test_notices_are_drained_once() -> None:
    clock = FakeClock()
    promoter = _promoter(clock)
    promoter.submit("d1", {})
    clock.advance(5.0)
    promoter.promote_due()
    first = promoter.notices()
    assert len(first) == 1
    assert first[0].to_dict()["status"] == "promoted"
    assert promoter.notices() == ()


def test_converge_records_acceptance_and_rejection() -> None:
    clock = FakeClock()
    promoter = _promoter(clock)
    promoter.submit("accepted", {})
    promoter.submit("denied", {})
    clock.advance(5.0)
    promoter.promote_due()

    converged = promoter.converge(
        "accepted", accepted=True, reason="async approval arrived"
    )
    assert converged.status == "converged"
    assert converged.reason == "async approval arrived"
    rejected = promoter.converge("denied", accepted=False, reason="policy denied")
    assert rejected.status == "rejected"

    again = promoter.converge("accepted", accepted=False)
    assert again == converged  # terminal status is kept

    notices = promoter.notices()
    assert [notice.status for notice in notices] == [
        "promoted",
        "promoted",
        "converged",
        "rejected",
    ]
    assert notices[2].elapsed_seconds == 5.0


def test_converge_works_before_promotion() -> None:
    clock = FakeClock()
    promoter = _promoter(clock)
    promoter.submit("d1", {})
    clock.advance(1.0)
    decision = promoter.converge("d1", accepted=True)
    assert decision.status == "converged"
    assert promoter.notices()[0].elapsed_seconds == 1.0
    assert promoter.promote_due() == ()


def test_subscribe_receives_notices_and_failures_are_contained() -> None:
    clock = FakeClock()
    promoter = _promoter(clock)
    seen: list[ConvergenceNotice] = []
    promoter.subscribe(seen.append)

    def explode(notice: ConvergenceNotice) -> None:
        raise RuntimeError("subscriber is broken")

    promoter.subscribe(explode)
    promoter.submit("d1", {})
    promoter.converge("d1", accepted=False, reason="no")

    assert [notice.status for notice in seen] == ["rejected"]
    assert promoter.notices()[0].status == "rejected"


def test_pending_capacity_evicts_oldest_pending_with_notice() -> None:
    clock = FakeClock()
    promoter = _promoter(clock, max_pending=2)
    promoter.submit("a", {})
    clock.advance(1.0)
    promoter.submit("b", {})
    clock.advance(1.0)
    promoter.submit("c", {})

    assert len(promoter) == 2
    assert [decision.decision_id for decision in promoter.pending()] == ["b", "c"]
    with pytest.raises(KeyError):
        promoter.check("a")
    notices = promoter.notices()
    assert len(notices) == 1
    assert notices[0].decision_id == "a"
    assert notices[0].status == "rejected"
    assert "capacity" in notices[0].reason


def test_pending_capacity_prefers_evicting_settled_decisions() -> None:
    clock = FakeClock()
    promoter = _promoter(clock, max_pending=2)
    promoter.submit("settled", {})
    promoter.converge("settled", accepted=True)
    promoter.notices()
    promoter.submit("bee", {})
    clock.advance(1.0)
    promoter.submit("cee", {})

    assert len(promoter) == 2
    with pytest.raises(KeyError):
        promoter.check("settled")
    assert [decision.decision_id for decision in promoter.pending()] == ["bee", "cee"]
    assert promoter.notices() == ()


def test_convergence_notices_are_capped_at_max_pending() -> None:
    clock = FakeClock()
    promoter = _promoter(clock, max_pending=2)
    for decision_id in ("a", "b", "c"):
        promoter.submit(decision_id, {})
        promoter.converge(decision_id, accepted=True)

    notices = promoter.notices()
    assert len(notices) == 2
    assert [notice.decision_id for notice in notices] == ["b", "c"]
    assert all(notice.status == "converged" for notice in notices)


def test_unknown_decisions_raise_key_error() -> None:
    promoter = _promoter(FakeClock())
    with pytest.raises(KeyError):
        promoter.check("missing")
    with pytest.raises(KeyError):
        promoter.converge("missing", accepted=True)


def test_invalid_constructor_and_decision_arguments() -> None:
    with pytest.raises(ValueError):
        SyncAsyncPromoter(threshold_seconds=-1.0)
    with pytest.raises(ValueError):
        SyncAsyncPromoter(max_pending=0)
    with pytest.raises(ValueError):
        PendingDecision(decision_id="", payload={}, created_at=0.0)
    with pytest.raises(ValueError):
        PendingDecision(decision_id="d1", payload={}, created_at=0.0, status="weird")


def test_decision_and_notice_dicts() -> None:
    decision = PendingDecision(decision_id="d1", payload={"k": 1}, created_at=2.0)
    assert decision.to_dict() == {
        "decision_id": "d1",
        "payload": {"k": 1},
        "created_at": 2.0,
        "status": "pending",
        "reason": "",
    }
    notice = ConvergenceNotice(
        decision_id="d1", status="promoted", elapsed_seconds=5.0, reason="late"
    )
    assert notice.to_dict() == {
        "decision_id": "d1",
        "status": "promoted",
        "elapsed_seconds": 5.0,
        "reason": "late",
    }


def test_defaults_and_threshold_property() -> None:
    promoter = SyncAsyncPromoter()
    assert promoter.threshold_seconds == 5.0
    assert promoter.max_pending == 256


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("0", False),
        ("off", False),
    ],
)
def test_promotion_enabled_reads_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("UC_SYNC_ASYNC_PROMOTION", value)
    assert promotion_enabled() is expected


def test_promotion_enabled_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_SYNC_ASYNC_PROMOTION", raising=False)
    assert promotion_enabled() is False
