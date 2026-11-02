"""The attention projection must encode the "少打扰" invariant, not just document it.

The canonical definition is ``crates/uc-types/src/governance/attention.rs``.
These tests pin the Python projection to it: an attention state never holds a
worker slot, only a pending human decision reserves human-priority capacity,
the worklist order is a deterministic total order, and resolution is
idempotent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from ultimate_coders.attention.model import (
    EPOCH,
    MAX_PRIORITY,
    AttentionItem,
    AttentionKind,
    Capacity,
    open_items,
    worklist_order,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _item(
    attention_id: str,
    case_id: str,
    kind: AttentionKind,
    *,
    since: datetime = NOW,
    priority: int = 0,
) -> AttentionItem:
    """Build a minimal open item for ordering / classification tests."""
    return AttentionItem(
        id=attention_id,
        case_id=case_id,
        kind=kind,
        summary="waiting on something",
        since=since,
        priority=priority,
    )


def _wire(**overrides: object) -> dict[str, object]:
    """Build one wire-shaped attention record with overridable fields."""
    payload: dict[str, object] = {
        "id": "att-1",
        "case_id": "case-1",
        "kind": "waiting_event",
        "summary": "waiting for CI",
        "since": "2026-01-01T00:00:00Z",
        "priority": 0,
    }
    payload.update(overrides)
    return payload


def test_waiting_for_ci_holds_no_worker_slot_and_reserves_nothing() -> None:
    item = _item("att-ci", "case-ci", AttentionKind.WAITING_EVENT)
    assert item.kind is AttentionKind.WAITING_EVENT
    assert item.holds_worker_slot() is False
    assert item.reserves_human_slot() is False
    assert item.requires_human_now() is False


def test_no_attention_kind_ever_holds_a_worker_slot() -> None:
    for kind in AttentionKind:
        assert kind.holds_worker_slot() is False, kind
        assert _item("att", "case", kind).holds_worker_slot() is False, kind


def test_only_needs_human_reserves_and_interrupts_a_human() -> None:
    reserving = {kind for kind in AttentionKind if kind.reserves_human_slot()}
    interrupting = {kind for kind in AttentionKind if kind.requires_human_now()}
    assert reserving == {AttentionKind.NEEDS_HUMAN}
    assert interrupting == {AttentionKind.NEEDS_HUMAN}


def test_worklist_puts_human_decisions_first_then_priority_then_age() -> None:
    human = _item("human", "case-human", AttentionKind.NEEDS_HUMAN, priority=0)
    blocked_high = _item("blocked-high", "case-bh", AttentionKind.BLOCKED, priority=9)
    waiting_old = _item(
        "waiting-old",
        "case-wo",
        AttentionKind.WAITING_EVENT,
        since=NOW - timedelta(days=1),
    )
    waiting_new = _item("waiting-new", "case-wn", AttentionKind.WAITING_EVENT)

    ordered = worklist_order([waiting_new, blocked_high, waiting_old, human])

    # A human decision outranks a higher priority; priority orders the rest; the
    # older `since` breaks the priority tie.
    assert [item.id for item in ordered] == [
        "human",
        "blocked-high",
        "waiting-old",
        "waiting-new",
    ]


def test_worklist_is_a_deterministic_total_order() -> None:
    first = _item("id-a", "case-b", AttentionKind.WAITING_EVENT, priority=4)
    second = _item("id-b", "case-a", AttentionKind.WAITING_EVENT, priority=4)
    third = _item("id-c", "case-a", AttentionKind.WAITING_EVENT, priority=4)

    forward = worklist_order([first, second, third])
    backward = worklist_order([third, second, first])

    assert [item.id for item in forward] == ["id-b", "id-c", "id-a"]
    assert [item.id for item in backward] == ["id-b", "id-c", "id-a"]


def test_worklist_order_does_not_mutate_the_input() -> None:
    items = [
        _item("b", "case-b", AttentionKind.BLOCKED),
        _item("a", "case-a", AttentionKind.BLOCKED),
    ]
    snapshot = list(items)
    worklist_order(items)
    assert items == snapshot


def test_resolve_is_idempotent() -> None:
    item = _item("att", "case", AttentionKind.NEEDS_HUMAN)
    assert item.is_open() is True

    item.resolve("human:alice:release/2.0", now=NOW)
    assert item.is_open() is False
    first = item.to_dict()

    # A re-delivered decision must not rewrite the recorded resolution.
    item.resolve("auto:late", now=NOW + timedelta(hours=1))
    assert item.to_dict() == first
    assert item.resolution_summary == "human:alice:release/2.0"


def test_item_round_trips_the_wire_shape() -> None:
    item = AttentionItem(
        id="att-1",
        case_id="case-1",
        kind=AttentionKind.NEEDS_HUMAN,
        summary="pick a branch",
        since=NOW,
        detail="which release?",
        priority=7,
        task_id="task-1",
        human_action="answer",
    )

    wire = item.to_dict()
    assert wire["kind"] == "needs_human"
    assert wire["since"] == "2026-01-01T00:00:00Z"
    assert wire["priority"] == 7
    assert "resolved_at" not in wire
    assert "resolution_summary" not in wire
    assert AttentionItem.from_dict(wire) == item

    item.resolve("human:alice:yes", now=NOW)
    wire = item.to_dict()
    assert wire["resolved_at"] == "2026-01-01T00:00:00Z"
    assert wire["resolution_summary"] == "human:alice:yes"
    assert AttentionItem.from_dict(wire) == item


def test_from_dict_rejects_missing_identity_and_unknown_kind() -> None:
    with pytest.raises(ValueError):
        AttentionItem.from_dict({"case_id": "case-1", "kind": "blocked"})
    with pytest.raises(ValueError):
        AttentionItem.from_dict({"id": "att-1", "kind": "blocked"})
    with pytest.raises(ValueError):
        AttentionItem.from_dict({"id": "att-1", "case_id": "case-1", "kind": "panic"})
    with pytest.raises(ValueError):
        AttentionItem.from_dict("not-an-object")  # type: ignore[arg-type]


def test_timestamps_parse_zulu_nanos_offsets_and_fall_back_to_epoch() -> None:
    nanos = AttentionItem.from_dict(_wire(since="2026-01-01T00:00:00.123456789Z"))
    assert nanos.since == datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)

    offset = AttentionItem.from_dict(_wire(since="2026-01-01T08:00:00+08:00"))
    assert offset.since == NOW

    naive = AttentionItem.from_dict(_wire(since="2026-01-01T00:00:00"))
    assert naive.since == NOW

    unix_seconds = AttentionItem.from_dict(_wire(since=1767225600))
    assert unix_seconds.since == NOW

    # A malformed timestamp is coerced to the epoch (sorts oldest) rather than
    # dropping the item, so one bad record cannot hide the worklist.
    broken = AttentionItem.from_dict(_wire(since="not-a-timestamp"))
    assert broken.since == EPOCH


def test_priority_is_clamped_into_the_rust_u8_range() -> None:
    assert AttentionItem.from_dict(_wire(priority=-5)).priority == 0
    assert AttentionItem.from_dict(_wire(priority=999)).priority == MAX_PRIORITY
    assert AttentionItem.from_dict(_wire(priority="nope")).priority == 0


def test_open_items_filters_resolved_items() -> None:
    resolved = _item("resolved", "case-a", AttentionKind.NEEDS_HUMAN)
    resolved.resolve("human:alice:yes", now=NOW)
    still_open = _item("open", "case-b", AttentionKind.WAITING_EVENT)

    assert [item.id for item in open_items([resolved, still_open])] == ["open"]


def test_capacity_available_is_never_negative() -> None:
    over_subscribed = Capacity(
        worker_slots_total=2,
        worker_slots_busy=2,
        worker_slots_reserved_human=2,
    )
    assert over_subscribed.worker_slots_available() == 0
    assert over_subscribed.can_admit_worker() is False
    assert Capacity(worker_slots_total=-4).worker_slots_total == 0


def test_slots_saved_by_quiet_waiting_is_waiting_plus_verifying() -> None:
    for waiting, verifying in ((0, 0), (1, 0), (0, 2), (3, 4)):
        capacity = Capacity(waiting_event_cases=waiting, verifying_cases=verifying)
        assert capacity.slots_saved_by_quiet_waiting() == waiting + verifying


def test_capacity_wire_shape_has_the_contract_fields() -> None:
    wire = Capacity(
        worker_slots_total=8,
        worker_slots_busy=5,
        worker_slots_reserved_human=1,
        waiting_event_cases=4,
        verifying_cases=1,
        needs_human_items=1,
        blocked_cases=2,
    ).to_dict()

    assert set(wire) == {
        "worker_slots_total",
        "worker_slots_busy",
        "worker_slots_reserved_human",
        "waiting_event_cases",
        "verifying_cases",
        "needs_human_items",
        "blocked_cases",
        "worker_slots_available",
        "slots_saved_by_quiet_waiting",
    }
    assert wire["worker_slots_available"] == 2
    assert wire["slots_saved_by_quiet_waiting"] == 5


def test_capacity_from_dict_recomputes_derived_fields() -> None:
    stale = {
        "worker_slots_total": 8,
        "worker_slots_busy": 5,
        "worker_slots_reserved_human": 1,
        "waiting_event_cases": 4,
        "verifying_cases": 1,
        "needs_human_items": 1,
        "blocked_cases": 0,
        # A stale reply must not be able to make the derived numbers disagree.
        "worker_slots_available": 99,
        "slots_saved_by_quiet_waiting": 99,
    }

    capacity = Capacity.from_dict(stale)

    assert capacity.worker_slots_available() == 2
    assert capacity.slots_saved_by_quiet_waiting() == 5
    assert Capacity.from_dict(None) == Capacity()


def test_kind_tokens_round_trip_and_reject_junk() -> None:
    for kind in AttentionKind:
        assert AttentionKind.from_str_lossy(kind.as_str()) is kind
        assert AttentionKind.from_str_lossy(f"  {kind.value.upper()}  ") is kind
        assert str(kind) == kind.value
    assert AttentionKind.from_str_lossy("waiting") is None
    assert AttentionKind.from_str_lossy(None) is None
    assert AttentionKind.from_str_lossy(7) is None


def test_worklist_visibility_matches_the_rust_definition() -> None:
    visible = {kind for kind in AttentionKind if kind.is_visible_in_worklist()}
    assert visible == {
        AttentionKind.WAITING_EVENT,
        AttentionKind.NEEDS_HUMAN,
        AttentionKind.BLOCKED,
    }
    assert AttentionKind.VERIFYING.is_visible_in_worklist() is False
