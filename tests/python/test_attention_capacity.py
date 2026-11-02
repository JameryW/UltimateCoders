"""The human reserve must be armed only by pending human work, and never negative.

``CapacityTracker`` is the local projection of the scheduler's capacity: it reads
a worker-registry snapshot plus the open attention items and produces the same
``Capacity`` shape the gateway reports. These tests pin the policy: a case
waiting on CI costs no worker slot and reserves nothing, a pending human
decision reserves exactly one slot (the configured floor, default ``1``), the
available count is clamped at zero, and
``slots_saved_by_quiet_waiting == waiting_event_cases + verifying_cases``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from ultimate_coders.attention.capacity import (
    DEFAULT_HUMAN_RESERVE,
    HUMAN_RESERVE_ENV,
    CapacityTracker,
    human_reserve_from_env,
    slots_from_registry,
)
from ultimate_coders.attention.model import AttentionItem, AttentionKind

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

REGISTRY = {
    "available": True,
    "workers": [
        {"id": "worker-1", "max_capacity": 4, "current_load": 2},
        {"id": "worker-2", "max_capacity": 4, "current_load": 1},
    ],
}


def _item(attention_id: str, case_id: str, kind: AttentionKind) -> AttentionItem:
    """Build a minimal open attention item."""
    return AttentionItem(
        id=attention_id,
        case_id=case_id,
        kind=kind,
        summary="parked",
        since=NOW,
    )


def test_waiting_for_ci_consumes_no_worker_slot_and_reserves_nothing() -> None:
    tracker = CapacityTracker(human_reserve=DEFAULT_HUMAN_RESERVE)

    capacity = tracker.snapshot(
        [_item("att-ci", "case-ci", AttentionKind.WAITING_EVENT)], REGISTRY
    )

    assert capacity.worker_slots_total == 8
    assert capacity.worker_slots_busy == 3
    # The case is parked, so nothing is held back for it: no worker slot (it is
    # not executing) and no human slot (there is no decision pending).
    assert capacity.worker_slots_reserved_human == 0
    assert capacity.worker_slots_available() == 5
    assert capacity.waiting_event_cases == 1
    assert capacity.slots_saved_by_quiet_waiting() == 1


def test_needs_human_reserves_exactly_one_human_slot() -> None:
    tracker = CapacityTracker(human_reserve=DEFAULT_HUMAN_RESERVE)

    capacity = tracker.snapshot(
        [_item("att-human", "case-pick", AttentionKind.NEEDS_HUMAN)], REGISTRY
    )

    assert capacity.needs_human_items == 1
    assert capacity.worker_slots_reserved_human == 1
    assert capacity.worker_slots_available() == 4


def test_each_pending_decision_reserves_its_own_slot() -> None:
    items = [
        _item(f"att-{index}", f"case-{index}", AttentionKind.NEEDS_HUMAN)
        for index in range(3)
    ]

    capacity = CapacityTracker(human_reserve=DEFAULT_HUMAN_RESERVE).snapshot(items, REGISTRY)

    assert capacity.needs_human_items == 3
    assert capacity.worker_slots_reserved_human == 3
    assert capacity.worker_slots_available() == 2  # 8 total - 3 busy - 3 reserved


def test_reserve_floor_applies_only_when_human_work_is_pending() -> None:
    tracker = CapacityTracker(human_reserve=3)

    idle = tracker.snapshot([_item("att-ci", "case-ci", AttentionKind.WAITING_EVENT)], REGISTRY)
    assert idle.worker_slots_reserved_human == 0

    pending = tracker.snapshot(
        [_item("att-human", "case-pick", AttentionKind.NEEDS_HUMAN)], REGISTRY
    )
    assert pending.worker_slots_reserved_human == 3


def test_reserve_is_capped_by_configured_capacity() -> None:
    tiny = {"workers": [{"id": "worker-1", "max_capacity": 1, "current_load": 0}]}
    items = [
        _item(f"att-{index}", f"case-{index}", AttentionKind.NEEDS_HUMAN)
        for index in range(3)
    ]

    capacity = CapacityTracker(human_reserve=5).snapshot(items, tiny)

    assert capacity.worker_slots_total == 1
    assert capacity.worker_slots_reserved_human == 1
    assert capacity.worker_slots_available() == 0


def test_available_is_never_negative() -> None:
    over_subscribed = {"worker_slots_total": 2, "worker_slots_busy": 5}

    capacity = CapacityTracker().snapshot(
        [_item("att-human", "case-pick", AttentionKind.NEEDS_HUMAN)], over_subscribed
    )

    assert capacity.worker_slots_available() == 0
    # Over-subscription is a real scheduling bug: report it, do not hide it.
    assert capacity.worker_slots_busy == 5


def test_slots_saved_by_quiet_waiting_equals_waiting_plus_verifying() -> None:
    items = [
        _item("att-ci-1", "case-ci-1", AttentionKind.WAITING_EVENT),
        _item("att-ci-2", "case-ci-2", AttentionKind.WAITING_EVENT),
        _item("att-verify", "case-verify", AttentionKind.VERIFYING),
        _item("att-blocked", "case-blocked", AttentionKind.BLOCKED),
        _item("att-human", "case-pick", AttentionKind.NEEDS_HUMAN),
    ]

    capacity = CapacityTracker().snapshot(items, REGISTRY)

    assert capacity.waiting_event_cases == 2
    assert capacity.verifying_cases == 1
    assert capacity.blocked_cases == 1
    assert capacity.slots_saved_by_quiet_waiting() == 3
    assert capacity.slots_saved_by_quiet_waiting() == (
        capacity.waiting_event_cases + capacity.verifying_cases
    )


def test_registry_snapshot_shapes() -> None:
    # The dashboard's per-worker snapshot is summed over the registered workers.
    assert slots_from_registry(REGISTRY) == (8, 3)
    # An already-aggregated gateway snapshot is used as-is.
    assert slots_from_registry({"worker_slots_total": 6, "worker_slots_busy": 2}) == (6, 2)
    # Partial / malformed snapshots count as zero instead of raising.
    assert slots_from_registry(None) == (0, 0)
    assert slots_from_registry({}) == (0, 0)
    assert slots_from_registry({"workers": "nope"}) == (0, 0)
    assert slots_from_registry(
        {"workers": [{"max_capacity": "3", "current_load": -1}, "junk"]}
    ) == (3, 0)


def test_resolved_items_do_not_count_and_duplicate_items_count_once() -> None:
    resolved = _item("att-resolved", "case-ci", AttentionKind.WAITING_EVENT)
    resolved.resolve("event:ci-42", now=NOW)
    open_items = [
        _item("att-1", "case-ci", AttentionKind.WAITING_EVENT),
        _item("att-2", "case-ci", AttentionKind.WAITING_EVENT),
    ]

    capacity = CapacityTracker().snapshot([resolved, *open_items], REGISTRY)

    # Two open items on one case are one parked case, not two.
    assert capacity.waiting_event_cases == 1
    assert capacity.needs_human_items == 0


def test_env_reserve_parsing_never_fails() -> None:
    assert DEFAULT_HUMAN_RESERVE == 1
    assert human_reserve_from_env({}) == DEFAULT_HUMAN_RESERVE
    assert human_reserve_from_env({HUMAN_RESERVE_ENV: "4"}) == 4
    assert human_reserve_from_env({HUMAN_RESERVE_ENV: " "}) == DEFAULT_HUMAN_RESERVE
    assert human_reserve_from_env({HUMAN_RESERVE_ENV: "many"}) == DEFAULT_HUMAN_RESERVE
    assert human_reserve_from_env({HUMAN_RESERVE_ENV: "-3"}) == 0


def test_tracker_from_env_uses_the_configured_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HUMAN_RESERVE_ENV, "2")

    tracker = CapacityTracker.from_env()
    capacity = tracker.snapshot(
        [_item("att-human", "case-pick", AttentionKind.NEEDS_HUMAN)], REGISTRY
    )

    assert tracker.human_reserve == 2
    assert capacity.worker_slots_reserved_human == 2


def test_tracker_without_registry_reports_zero_slots() -> None:
    capacity = CapacityTracker().snapshot([_item("att-ci", "case-ci", AttentionKind.WAITING_EVENT)])

    assert capacity.worker_slots_total == 0
    assert capacity.worker_slots_busy == 0
    assert capacity.worker_slots_available() == 0
    assert capacity.waiting_event_cases == 1
