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


# ── F52: worker liveness refresh in the heartbeat tick ────────────


async def test_heartbeat_loop_refreshes_worker_liveness():
    """CRITICAL regression: Worker._last_heartbeat_at was only refreshed
    inside Worker.send_heartbeat() — which had ZERO callers — so the stall
    detector re-dispatched every subtask running >90s while still executing
    (duplicate worktrees + duplicate result events). The heartbeat tick must
    refresh it on every pass.
    """
    import asyncio

    nw = _make_worker()
    nw._running = True
    worker = MagicMock()
    worker.send_heartbeat = AsyncMock(return_value={})
    nw._worker = worker
    nw._publisher = None  # skip the NATS block
    nw._orchestrator = None
    nw._grpc_reg_engine = None

    task = asyncio.create_task(nw._heartbeat_loop())
    await asyncio.sleep(0.05)  # tick body runs before the 30s sleep
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert worker.send_heartbeat.await_count >= 1


async def test_worker_send_heartbeat_refreshes_timestamp():
    """The liveness stamp must actually advance."""
    import asyncio

    from ultimate_coders.agent.worker import Worker

    w = Worker(worker_id="test-w")
    original = w._last_heartbeat_at
    await asyncio.sleep(0.01)
    info = await w.send_heartbeat()
    assert w._last_heartbeat_at > original
    assert info["worker_id"] == "test-w"


# ── F53: consecutive heartbeat failures force re-registration ─────


async def _run_one_heartbeat_tick(nw: _NatsWorker) -> None:
    import asyncio

    task = asyncio.create_task(nw._heartbeat_loop())
    await asyncio.sleep(0.05)  # one tick completes; loop then sleeps 30s
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _make_heartbeat_worker(hb_ok: bool) -> _NatsWorker:
    nw = _make_worker()
    nw._running = True
    worker = MagicMock()
    worker.send_heartbeat = AsyncMock(return_value={})
    worker.worker_id = "w-1"
    worker.get_info.return_value = MagicMock(current_load=0)
    nw._worker = worker
    nw._publisher = MagicMock()
    nw._publisher.publish_heartbeat = AsyncMock()
    nw._orchestrator = None
    nw._grpc_endpoint = ""  # no re-registration endpoint in this test
    engine = MagicMock()
    engine.worker_heartbeat_async = AsyncMock(return_value=hb_ok)
    nw._grpc_reg_engine = engine
    return nw


async def test_heartbeat_failure_threshold_forces_reregistration(monkeypatch):
    """After 3 consecutive gateway heartbeat failures the registration engine
    is cleared, so the next tick takes the existing re-registration path —
    instead of silently staying 'registered' while the gateway dropped us.
    """
    monkeypatch.delenv("UC_GRPC_ENDPOINT", raising=False)
    nw = _make_heartbeat_worker(hb_ok=False)
    nw._consecutive_heartbeat_failures = 2  # next failure hits the threshold
    await _run_one_heartbeat_tick(nw)
    assert nw._consecutive_heartbeat_failures == 3
    assert nw._grpc_reg_engine is None


async def test_heartbeat_success_resets_failure_counter(monkeypatch):
    monkeypatch.delenv("UC_GRPC_ENDPOINT", raising=False)
    nw = _make_heartbeat_worker(hb_ok=True)
    nw._consecutive_heartbeat_failures = 2
    await _run_one_heartbeat_tick(nw)
    assert nw._consecutive_heartbeat_failures == 0
    assert nw._grpc_reg_engine is not None


# ── F54: NATS callback must not await execution ───────────────────


async def test_handle_subtask_execute_dispatches_to_background():
    """HIGH regression: nats-py awaits the subscription callback inline on
    its single reader task, so awaiting execute_subtask in the callback
    serialized all subtasks (max_capacity dead) and queued messages past
    pending_msgs_limit were silently dropped. The callback must hand the
    execution to a background task and return immediately.
    """
    import asyncio

    nw = _make_worker()
    nw._running = True
    nw._dispatch_event = asyncio.Event()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_execute(subtask):
        started.set()
        await release.wait()
        result = MagicMock()
        result.success = True
        result.summary = "done"
        result.modified_files = []
        return result

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.capabilities = []
    worker.execute_subtask = slow_execute
    nw._worker = worker
    nw._publisher = MagicMock()
    nw._publisher.publish_event = AsyncMock()
    nw._publisher.publish_update = AsyncMock()

    msg = MagicMock()
    msg.data = json.dumps({
        "task_id": "t-1",
        "subtask_id": "st-1",
        "description": "do the thing",
        "timeout_seconds": 600,
        "dispatch_mode": "prefer_remote",
        "steps": [],
    }).encode()

    # Callback must return while execution is still blocked on `release`.
    # (wait_for succeeding at all proves the callback didn't await the
    # execution; the poll gives the spawned task its first loop tick.)
    await asyncio.wait_for(nw._handle_subtask_execute(msg), timeout=2)
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set(), "execution never started"
    assert not release.is_set(), "callback waited for execution to finish"

    # Let the background task finish and publish the result.
    release.set()
    for _ in range(50):
        if nw._publisher.publish_event.await_count >= 1:
            break
        await asyncio.sleep(0.02)
    assert nw._publisher.publish_event.await_count >= 1
    published_type = nw._publisher.publish_event.await_args.args[0]
    assert published_type == "subtask_completed"


# ── F56: remote subtask timeout reclaim ───────────────────


async def test_reclaim_timed_out_remote_subtasks():
    """Remote-assigned subtasks with no result past timeout+grace reset to
    PENDING; fresh dispatches are untouched. 'remote' is never a real worker
    entry, so the stale-worker sweep could never see these before.
    """
    import asyncio
    from datetime import datetime, timedelta, timezone

    from ultimate_coders.agent.orchestrator import Orchestrator
    from ultimate_coders.agent.types import (
        Subtask,
        SubtaskStatus,
        Task,
        TaskStatus,
    )

    nw = _make_worker()
    orch = Orchestrator()
    st = Subtask(id="st-r1", description="remote one", parent_id="t-1", timeout_seconds=600)
    st.status = SubtaskStatus.ASSIGNED
    st.assigned_worker = "remote"
    task = Task(id="t-1", description="t", subtasks=[st], status=TaskStatus.IN_PROGRESS)
    orch.tasks["t-1"] = task
    nw._orchestrator = orch
    nw._publisher = MagicMock()
    nw._publisher.publish_event = AsyncMock()
    nw._dispatch_event = asyncio.Event()

    # Expired: 700s > 600 timeout + 60s grace → reclaimed
    nw._remote_dispatched_at["st-r1"] = datetime.now(timezone.utc) - timedelta(seconds=700)
    await nw._reclaim_timed_out_remote_subtasks()
    assert st.status == SubtaskStatus.PENDING
    assert st.assigned_worker is None
    assert st.retry_count == 1
    assert "st-r1" not in nw._remote_dispatched_at
    assert nw._publisher.publish_event.await_count == 1

    # Fresh dispatch (100s < deadline) → untouched
    st2 = Subtask(id="st-r2", description="remote two", parent_id="t-1", timeout_seconds=600)
    st2.status = SubtaskStatus.ASSIGNED
    st2.assigned_worker = "remote"
    task.subtasks.append(st2)
    nw._remote_dispatched_at["st-r2"] = datetime.now(timezone.utc) - timedelta(seconds=100)
    await nw._reclaim_timed_out_remote_subtasks()
    assert st2.status == SubtaskStatus.ASSIGNED
    assert st2.assigned_worker == "remote"


async def test_dispatch_remote_publish_failure_resets_to_pending():
    """A failed NATS publish must not leave the subtask ASSIGNED to 'remote'
    forever — reset to PENDING so it gets re-selected.
    """
    from ultimate_coders.agent.orchestrator import Orchestrator
    from ultimate_coders.agent.types import (
        Subtask,
        SubtaskStatus,
        Task,
        TaskStatus,
    )

    nw = _make_worker()
    orch = Orchestrator()
    st = Subtask(id="st-d1", description="d", parent_id="t-2")
    task = Task(id="t-2", description="t", subtasks=[st], status=TaskStatus.IN_PROGRESS)
    orch.tasks["t-2"] = task
    nw._orchestrator = orch
    nw._publisher = MagicMock()
    nw._nc = MagicMock()
    nw._nc.publish = AsyncMock(side_effect=Exception("nats down"))

    await nw._dispatch_remote(st)

    assert st.status == SubtaskStatus.PENDING
    assert st.assigned_worker is None
    assert "st-d1" not in nw._remote_dispatched_at


# ── _build_snapshot task timestamps ─────────────────────────────


async def test_build_snapshot_emits_real_task_timestamps():
    """_build_snapshot hardcoded created_at/updated_at to 0 → dashboard showed
    epoch. Regression: must emit int(t.<field>.timestamp()) (tz-aware UTC)."""
    from datetime import datetime, timezone

    nw = _make_worker()
    nw._dash_listworkers = AsyncMock(return_value={"workers": []})
    nw._dash_getschedulerstatus = AsyncMock(return_value={"available": False})

    created = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 7, 20, 12, 5, 0, tzinfo=timezone.utc)
    task = Task(
        id="t1",
        description="d",
        status=TaskStatus.CREATED,
        created_at=created,
        updated_at=updated,
    )

    orch = MagicMock()
    orch.engine = None
    orch.tasks = {"t1": task}
    orch._dashboard_app = None
    nw._orchestrator = orch

    snap = await nw._build_snapshot()

    entry = snap["tasks"]["tasks"][0]
    assert entry["created_at"] == int(created.timestamp())
    assert entry["updated_at"] == int(updated.timestamp())
    assert entry["created_at"] != 0


# ── stop() shutdown reporting ──────────────────────────────────


async def test_stop_reports_in_progress_subtask_no_attrerror():
    """stop() referenced SubtaskStatus.RUNNING (nonexistent member) →
    AttributeError aborted the shutdown reporting loop, leaving in-flight
    subtasks IN_PROGRESS forever. Regression: IN_PROGRESS subtasks owned by
    this worker must be reported as failed on shutdown, no AttributeError."""
    from ultimate_coders.agent.orchestrator import Orchestrator
    from ultimate_coders.agent.types import TaskStatus

    nw = _make_worker()
    nw._orchestrator = Orchestrator()

    worker = MagicMock()
    worker.worker_id = "w-1"
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    nw._publisher = publisher

    # Suppress the real handle_subtask_result side effects beyond status flip.
    nw._orchestrator.handle_subtask_result = AsyncMock(
        side_effect=lambda r: _flip_subtask_failed(nw._orchestrator, r.subtask_id)
    )

    st = Subtask(id="st-run", description="d", status=SubtaskStatus.IN_PROGRESS)
    st.assigned_worker = "w-1"
    task = Task(
        id="t1",
        description="d",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[st],
    )
    nw._orchestrator.tasks[task.id] = task

    # Must not raise AttributeError (SubtaskStatus.RUNNING did).
    await nw.stop()

    publisher.publish_event.assert_awaited()
    # The subtask was reported (handle_subtask_result called for it).
    assert any(
        call.args and call.args[0].subtask_id == "st-run"
        for call in nw._orchestrator.handle_subtask_result.await_args_list
    )


def _flip_subtask_failed(orch, subtask_id):
    """Test helper: flip the subtask to FAILED (mirrors real handler outcome)."""
    for t in orch.tasks.values():
        for st in t.subtasks:
            if st.id == subtask_id:
                st.status = SubtaskStatus.FAILED
                return
