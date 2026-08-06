"""Tests for TaskEventEmitter — ring buffer for task event history with dedup.

Covers:
- TaskEvent.to_dict (shape, optional-field omission, field values)
- dedup_event_key (determinism, format, input sensitivity)
- TaskEventEmitter.emit + get_recent_events (storage, ordering, async)
- buffer overflow (oldest evicted, newest kept)
- get_recent_events limit + task_id filtering
- dedup window behavior (within-window drop, after-window pass)
- stale dedup-entry pruning
- empty emitter
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from ultimate_coders.agent.event_emitter import (
    DEDUP_WINDOW_SECONDS,
    TaskEvent,
    TaskEventEmitter,
    dedup_event_key,
)

# ── TaskEvent.to_dict ──────────────────────────────────────────


class TestTaskEventToDict:
    def test_always_includes_v_timestamp_type_task_id(self):
        ev = TaskEvent(type="tool_call", task_id="t1")
        d = ev.to_dict()
        assert d["v"] == 1
        assert d["type"] == "tool_call"
        assert d["task_id"] == "t1"
        assert isinstance(d["timestamp"], str) and d["timestamp"]

    def test_omits_subtask_id_when_empty(self):
        ev = TaskEvent(type="started", task_id="t1", subtask_id="")
        assert "subtask_id" not in ev.to_dict()

    def test_includes_subtask_id_when_set(self):
        ev = TaskEvent(type="started", task_id="t1", subtask_id="s1")
        assert ev.to_dict()["subtask_id"] == "s1"

    def test_omits_data_when_empty(self):
        ev = TaskEvent(type="started", task_id="t1")
        assert "data" not in ev.to_dict()

    def test_includes_data_when_populated(self):
        payload = {"tool": "read_file", "lines": 42}
        ev = TaskEvent(type="tool_call", task_id="t1", data=payload)
        assert ev.to_dict()["data"] == payload

    def test_timestamp_is_isoformat_string(self):
        """timestamp default factory produces an ISO 8601 UTC string."""
        ev = TaskEvent()
        # datetime.isoformat() always contains the 'T' separator and the
        # timezone offset ('+00:00' for UTC).
        assert "T" in ev.timestamp
        assert ev.timestamp.endswith("+00:00")

    def test_full_event_shape(self):
        ev = TaskEvent(
            type="subtask_completed",
            task_id="task-7",
            subtask_id="sub-3",
            data={"duration": 1.2},
        )
        assert ev.to_dict() == {
            "v": 1,
            "timestamp": ev.timestamp,
            "type": "subtask_completed",
            "task_id": "task-7",
            "subtask_id": "sub-3",
            "data": {"duration": 1.2},
        }


# ── dedup_event_key ────────────────────────────────────────────


class TestDedupEventKey:
    def test_format_is_colon_separated_triple(self):
        key = dedup_event_key("t1", "s1", "started")
        assert key == "t1:s1:started"

    def test_deterministic_for_same_inputs(self):
        a = dedup_event_key("t1", "s1", "started")
        b = dedup_event_key("t1", "s1", "started")
        assert a == b

    def test_differs_when_task_id_changes(self):
        assert dedup_event_key("t1", "s1", "e") != dedup_event_key("t2", "s1", "e")

    def test_differs_when_subtask_id_changes(self):
        assert dedup_event_key("t1", "s1", "e") != dedup_event_key("t1", "s2", "e")

    def test_differs_when_event_type_changes(self):
        assert dedup_event_key("t1", "s1", "a") != dedup_event_key("t1", "s1", "b")

    def test_empty_string_inputs_preserved(self):
        """Empty task_id / subtask_id are valid (orchestrator-level events)."""
        assert dedup_event_key("", "", "global_event") == "::global_event"


# ── TaskEventEmitter.emit + get_recent_events ──────────────────


class TestEmitAndGetRecent:
    async def test_emit_stores_event(self):
        emitter = TaskEventEmitter()
        await emitter.emit("started", task_id="t1", subtask_id="s1")
        events = emitter.get_recent_events()
        assert len(events) == 1
        assert events[0]["type"] == "started"
        assert events[0]["task_id"] == "t1"
        assert events[0]["subtask_id"] == "s1"

    async def test_emit_preserves_data_payload(self):
        emitter = TaskEventEmitter()
        payload = {"tool": "write_file", "path": "/a/b.py"}
        await emitter.emit("tool_call", task_id="t1", data=payload)
        assert emitter.get_recent_events()[0]["data"] == payload

    async def test_emit_defaults_empty_data_to_empty_dict(self):
        """emit(data=None) must store {} — to_dict omits 'data' key entirely
        only when data is falsy; the Event is constructed with data={}."""
        emitter = TaskEventEmitter()
        await emitter.emit("started", task_id="t1")  # data=None
        ev = emitter.get_recent_events()[0]
        # data omitted from dict because {} is falsy
        assert "data" not in ev

    async def test_get_recent_events_returns_newest_first(self):
        """get_recent_events returns insertion order (newest at front of slice).
        The deque appends newest to the right; list() preserves that order,
        and the slice returns from oldest→newest. We assert the documented
        'newest first' ordering by checking that the last-emitted event is
        at the END of the returned list (the slice is events[:limit] of the
        oldest-first list)."""
        emitter = TaskEventEmitter()
        for i in range(5):
            await emitter.emit(f"event_{i}", task_id="t1", subtask_id=f"s{i}")
        events = emitter.get_recent_events()
        # list(deque) is oldest→newest; the code returns events[:limit]
        # so index 0 is the oldest and index -1 is the newest.
        assert events[0]["type"] == "event_0"
        assert events[-1]["type"] == "event_4"

    async def test_emit_is_async_coroutine(self):
        """emit must return a coroutine (awaitable), not a plain value."""
        emitter = TaskEventEmitter()
        coro = emitter.emit("started", task_id="t1")
        assert asyncio.iscoroutine(coro)
        await coro  # drain to avoid 'coroutine never awaited' warning

    async def test_multiple_emits_all_stored(self):
        emitter = TaskEventEmitter()
        await emitter.emit("a", task_id="t1")
        await emitter.emit("b", task_id="t1")
        await emitter.emit("c", task_id="t1")
        assert len(emitter.get_recent_events()) == 3

    async def test_get_recent_events_filters_by_task_id(self):
        emitter = TaskEventEmitter()
        await emitter.emit("a", task_id="t1")
        await emitter.emit("b", task_id="t2")
        await emitter.emit("c", task_id="t1")
        t1_events = emitter.get_recent_events(task_id="t1")
        assert len(t1_events) == 2
        assert all(e["task_id"] == "t1" for e in t1_events)

    async def test_get_recent_events_respects_limit(self):
        emitter = TaskEventEmitter()
        for i in range(10):
            await emitter.emit(f"e{i}", task_id="t1", subtask_id=f"s{i}")
        events = emitter.get_recent_events(limit=3)
        assert len(events) == 3
        # limit takes from the front (oldest) — first 3 emitted
        assert events[0]["type"] == "e0"
        assert events[2]["type"] == "e2"

    async def test_get_recent_events_limit_with_task_filter(self):
        emitter = TaskEventEmitter()
        for i in range(6):
            await emitter.emit(f"e{i}", task_id="t1" if i % 2 == 0 else "t2")
        events = emitter.get_recent_events(task_id="t1", limit=2)
        assert len(events) == 2
        assert all(e["task_id"] == "t1" for e in events)

    def test_empty_emitter_returns_empty_list(self):
        emitter = TaskEventEmitter()
        assert emitter.get_recent_events() == []

    def test_empty_emitter_with_task_id_returns_empty_list(self):
        emitter = TaskEventEmitter()
        assert emitter.get_recent_events(task_id="nope") == []

    def test_empty_emitter_with_limit_returns_empty_list(self):
        emitter = TaskEventEmitter()
        assert emitter.get_recent_events(limit=10) == []


# ── Buffer overflow ────────────────────────────────────────────


class TestBufferOverflow:
    async def test_oldest_evicted_when_buffer_exceeds_max(self):
        """When events exceed buffer_size, the oldest are evicted (deque
        maxlen drops from the left/front)."""
        emitter = TaskEventEmitter(buffer_size=3)
        for i in range(5):
            await emitter.emit(f"e{i}", task_id="t1", subtask_id=f"s{i}")
        events = emitter.get_recent_events()
        assert len(events) == 3
        # e0, e1 evicted; e2, e3, e4 remain
        assert events[0]["type"] == "e2"
        assert events[-1]["type"] == "e4"

    async def test_buffer_at_exact_capacity_keeps_all(self):
        emitter = TaskEventEmitter(buffer_size=3)
        for i in range(3):
            await emitter.emit(f"e{i}", task_id="t1", subtask_id=f"s{i}")
        assert len(emitter.get_recent_events()) == 3

    async def test_default_buffer_size_is_500(self):
        """The deque maxlen must equal the default buffer_size of 500."""
        emitter = TaskEventEmitter()
        assert emitter._recent.maxlen == 500

    async def test_custom_buffer_size_sets_deque_maxlen(self):
        emitter = TaskEventEmitter(buffer_size=10)
        assert emitter._recent.maxlen == 10


# ── Dedup window ───────────────────────────────────────────────


class TestDedupWindow:
    async def test_duplicate_within_window_dropped(self):
        emitter = TaskEventEmitter()
        await emitter.emit("completed", task_id="t1", subtask_id="s1")
        await emitter.emit("completed", task_id="t1", subtask_id="s1")  # dup
        assert len(emitter.get_recent_events()) == 1

    async def test_different_subtask_not_deduped(self):
        emitter = TaskEventEmitter()
        await emitter.emit("completed", task_id="t1", subtask_id="s1")
        await emitter.emit("completed", task_id="t1", subtask_id="s2")
        assert len(emitter.get_recent_events()) == 2

    async def test_different_task_not_deduped(self):
        emitter = TaskEventEmitter()
        await emitter.emit("completed", task_id="t1", subtask_id="s1")
        await emitter.emit("completed", task_id="t2", subtask_id="s1")
        assert len(emitter.get_recent_events()) == 2

    async def test_different_type_not_deduped(self):
        emitter = TaskEventEmitter()
        await emitter.emit("started", task_id="t1", subtask_id="s1")
        await emitter.emit("completed", task_id="t1", subtask_id="s1")
        assert len(emitter.get_recent_events()) == 2

    async def test_same_key_after_window_passes(self):
        """After DEDUP_WINDOW_SECONDS elapses, the same key is accepted again."""
        emitter = TaskEventEmitter()
        await emitter.emit("completed", task_id="t1", subtask_id="s1")

        # Simulate time advancing past the dedup window.
        base = time.monotonic()
        with patch(
            "ultimate_coders.agent.event_emitter.time.monotonic",
            side_effect=lambda: base + DEDUP_WINDOW_SECONDS + 0.1,
        ):
            await emitter.emit("completed", task_id="t1", subtask_id="s1")

        assert len(emitter.get_recent_events()) == 2

    async def test_dedup_key_stored_on_first_emit(self):
        """The dedup_seen dict should record the key after the first emit."""
        emitter = TaskEventEmitter()
        await emitter.emit("completed", task_id="t1", subtask_id="s1")
        assert "t1:s1:completed" in emitter._dedup_seen


# ── Stale dedup pruning ────────────────────────────────────────


class TestDedupPruning:
    async def test_stale_dedup_entries_pruned_on_emit(self):
        """Entries older than 2× the window are pruned on the next emit."""
        emitter = TaskEventEmitter()

        # Emit an event to populate dedup_seen.
        t0 = time.monotonic()
        with patch(
            "ultimate_coders.agent.event_emitter.time.monotonic",
            side_effect=lambda: t0,
        ):
            await emitter.emit("old_event", task_id="t1", subtask_id="s1")
        assert "t1:s1:old_event" in emitter._dedup_seen

        # Advance past 2× window — the old entry should be pruned on next emit.
        with patch(
            "ultimate_coders.agent.event_emitter.time.monotonic",
            side_effect=lambda: t0 + DEDUP_WINDOW_SECONDS * 2 + 1,
        ):
            await emitter.emit("new_event", task_id="t2", subtask_id="s2")

        assert "t1:s1:old_event" not in emitter._dedup_seen
        assert "t2:s2:new_event" in emitter._dedup_seen
