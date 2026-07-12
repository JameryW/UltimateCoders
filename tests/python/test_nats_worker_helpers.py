"""Tests for NatsWorker pure helpers (no NATS connection required).

Covers regression bugs fixed in the agent-deep-analysis loop:
- _load_js_seq: read_memory returns a MemoryEntry (not a str); int() on the
  object raised TypeError → returned 0 → JetStream seq never persisted →
  every restart skipped event replay.
- _reset_subtask_to_pending: a dispatch-rejected subtask was left ASSIGNED
  (assign_subtask set it ASSIGNED, select_next_subtask only returns PENDING)
  → stuck forever. Must reset to PENDING.
- modified_files round-trip: remote result events must include
  modified_files so remote file changes reach aggregation/merge arbitration.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.agent.types import (
    Subtask,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkflowStep,
)
from ultimate_coders.nats_worker import NatsWorker as _NatsWorker


def _make_worker() -> _NatsWorker:
    """Build a NatsWorker without running start() (no NATS/IO)."""
    return _NatsWorker(project_path="/tmp/test", mode="default")


# ── _load_js_seq ────────────────────────────────────────────────


def test_load_js_seq_extracts_content_from_memory_entry():
    """read_memory returns a MemoryEntry; int(MemoryEntry) raised TypeError.

    Regression: returned 0 → JetStream last-acked seq never persisted →
    every restart treated as first start, skipping event replay.
    """
    nw = _make_worker()

    engine = MagicMock()
    entry = MagicMock()
    entry.content = "42"
    # Simulate the object-return path (real read_memory returns MemoryEntry).
    engine.read_memory.return_value = entry
    nw._engine = engine

    assert nw._load_js_seq() == 42


def test_load_js_seq_handles_string_return():
    """Some fallback paths return a plain str — still parse to int."""
    nw = _make_worker()
    engine = MagicMock()
    engine.read_memory.return_value = "7"
    nw._engine = engine
    assert nw._load_js_seq() == 7


def test_load_js_seq_returns_zero_when_no_engine():
    nw = _make_worker()
    nw._engine = None
    assert nw._load_js_seq() == 0


def test_load_js_seq_returns_zero_on_missing_key():
    nw = _make_worker()
    engine = MagicMock()
    engine.read_memory.return_value = None
    nw._engine = engine
    assert nw._load_js_seq() == 0


# ── _reset_subtask_to_pending ───────────────────────────────────


def test_reset_subtask_to_pending_resets_assigned_subtask():
    """A dispatch-rejected subtask was ASSIGNED; must reset to PENDING."""
    from ultimate_coders.agent.orchestrator import Orchestrator

    nw = _make_worker()
    nw._orchestrator = Orchestrator()

    st = Subtask(id="st-rejected", description="d", status=SubtaskStatus.ASSIGNED)
    st.assigned_worker = "remote"
    task = Task(
        id="t1", description="d", project_id="p",
        status=TaskStatus.IN_PROGRESS, subtasks=[st],
    )
    nw._orchestrator.tasks[task.id] = task

    assert nw._reset_subtask_to_pending("st-rejected") is True
    assert st.status == SubtaskStatus.PENDING
    assert st.assigned_worker is None


def test_reset_subtask_to_pending_skips_non_assigned():
    """Only ASSIGNED subtasks reset; a PENDING/COMPLETED one is untouched."""
    from ultimate_coders.agent.orchestrator import Orchestrator

    nw = _make_worker()
    nw._orchestrator = Orchestrator()

    st = Subtask(id="st-done", description="d", status=SubtaskStatus.COMPLETED)
    task = Task(
        id="t1", description="d", project_id="p",
        status=TaskStatus.IN_PROGRESS, subtasks=[st],
    )
    nw._orchestrator.tasks[task.id] = task

    assert nw._reset_subtask_to_pending("st-done") is False
    assert st.status == SubtaskStatus.COMPLETED


def test_reset_subtask_to_pending_unknown_id_returns_false():
    from ultimate_coders.agent.orchestrator import Orchestrator

    nw = _make_worker()
    nw._orchestrator = Orchestrator()
    assert nw._reset_subtask_to_pending("nope") is False


# ── _dispatch_remote includes steps ─────────────────────────────


def test_dispatch_remote_serializes_steps_in_nats_payload():
    """Regression: _dispatch_remote omitted `steps` from the NATS JSON.

    Python-dispatched remote subtasks lost their workflow chain (steps
    silently dropped). Rust server dispatch (NatsSubtaskExecute) already
    included steps. The fix adds `"steps": [s.to_dict() for s in subtask.steps]`.
    """
    nw = _make_worker()

    # Mock orchestrator so assign_subtask doesn't crash.
    nw._orchestrator = MagicMock()
    nw._orchestrator.assign_subtask = AsyncMock()
    nw._orchestrator.conflict_detector = MagicMock()

    # Mock NATS client to capture the published payload.
    captured: dict[str, bytes] = {}

    class FakeNc:
        async def publish(self, subject: str, payload: bytes) -> None:
            captured["subject"] = subject
            captured["payload"] = payload

    nw._nc = FakeNc()  # type: ignore[assignment]
    nw._publisher = MagicMock()  # truthy so the early return is skipped

    # Subtask with 2 steps — must round-trip through the NATS payload.
    steps = [
        WorkflowStep(
            agent="claude-code",
            prompt="Implement feature X",
            abort_on_failure=True,
        ),
        WorkflowStep(
            agent="codex",
            prompt="CR the implementation. {{prev_summary}}",
            abort_on_failure=False,
        ),
    ]
    subtask = Subtask(
        id="st-1",
        description="test subtask",
        parent_id="t-1",
        steps=steps,
    )

    import asyncio

    asyncio.run(nw._dispatch_remote(subtask))

    assert "payload" in captured, "NATS publish was not called"
    payload = json.loads(captured["payload"])
    assert "steps" in payload, "steps key missing from dispatch payload"
    assert len(payload["steps"]) == 2
    assert payload["steps"][0]["agent"] == "claude-code"
    assert payload["steps"][0]["prompt"] == "Implement feature X"
    assert payload["steps"][0]["abort_on_failure"] is True
    assert payload["steps"][1]["agent"] == "codex"
    assert payload["steps"][1]["abort_on_failure"] is False
