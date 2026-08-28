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

import asyncio
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkflowStep,
)
from ultimate_coders.nats_worker import (
    NatsPublisher,
    _make_task_event_payload,
    _make_task_update_payload,
)
from ultimate_coders.nats_worker import NatsWorker as _NatsWorker


def _make_worker() -> _NatsWorker:
    """Build a NatsWorker without running start() (no NATS/IO)."""
    return _NatsWorker(project_path="/tmp/test", mode="default")


def test_task_update_message_id_changes_with_snapshot_state():
    """Distinct snapshots in one dedup window must not collapse."""
    task = Task(id="t-update", description="d", project_id="p", status=TaskStatus.IN_PROGRESS)
    failed = Task(id="t-update", description="d", project_id="p", status=TaskStatus.FAILED)

    in_progress_id = _make_task_update_payload(task)["message_id"]
    failed_id = _make_task_update_payload(failed)["message_id"]

    assert in_progress_id != failed_id
    assert in_progress_id == _make_task_update_payload(task)["message_id"]


def test_partial_task_update_is_marked_explicitly():
    """Worker-only subtask updates must not imply a complete parent snapshot."""
    task = Task(id="t-partial", description="d", status=TaskStatus.IN_PROGRESS)

    full = _make_task_update_payload(task)
    partial = _make_task_update_payload(task, partial=True)

    assert full["partial"] is False
    assert partial["partial"] is True
    assert "description" in full
    assert "project_id" in full
    assert "description" not in partial
    assert "project_id" not in partial
    assert full["message_id"] != partial["message_id"]


def test_complete_task_update_carries_rehydration_metadata():
    """A gateway restart can rebuild a task from the next full snapshot."""
    task = Task(
        id="t-rehydrate",
        description="restore the task",
        project_id="project-a",
        status=TaskStatus.IN_PROGRESS,
    )

    payload = _make_task_update_payload(task)

    assert payload["description"] == "restore the task"
    assert payload["project_id"] == "project-a"


@pytest.mark.asyncio
async def test_task_snapshot_request_replies_with_complete_snapshots():
    """Gateway recovery receives complete snapshots owned by the Orchestrator."""
    nw = _make_worker()
    task = Task(
        id="t-recover",
        description="restore the task",
        project_id="project-a",
        status=TaskStatus.IN_PROGRESS,
    )
    nw._orchestrator = MagicMock(tasks={task.id: task})
    msg = MagicMock(reply="inbox")
    msg.respond = AsyncMock()

    await nw._handle_task_snapshot_request(msg)

    msg.respond.assert_awaited_once()
    payload = json.loads(msg.respond.call_args.args[0])
    assert payload["v"] == 1
    assert len(payload["tasks"]) == 1
    snapshot = payload["tasks"][0]
    assert snapshot["partial"] is False
    assert snapshot["description"] == "restore the task"
    assert snapshot["project_id"] == "project-a"


@pytest.mark.asyncio
async def test_task_snapshot_request_without_reply_is_ignored():
    """A plain subscription message must not attempt a response."""
    nw = _make_worker()
    msg = MagicMock(reply="")
    msg.respond = AsyncMock()

    await nw._handle_task_snapshot_request(msg)

    msg.respond.assert_not_awaited()


def test_task_update_message_id_changes_with_subtask_snapshot_state():
    """A subtask-only transition must reach Rust instead of being deduped."""
    st = Subtask(id="st-update", description="d", status=SubtaskStatus.PENDING)
    task = Task(
        id="t-update-subtask",
        description="d",
        project_id="p",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[st],
    )

    pending_id = _make_task_update_payload(task)["message_id"]
    st.status = SubtaskStatus.IN_PROGRESS
    in_progress_id = _make_task_update_payload(task)["message_id"]

    assert pending_id != in_progress_id


@pytest.mark.asyncio
async def test_nats_publisher_reports_transport_failure_without_raising():
    """The publisher keeps graceful degradation but exposes failure to callers."""
    nc = MagicMock()
    nc.publish = AsyncMock(side_effect=ConnectionError("NATS down"))
    publisher = NatsPublisher(nc)

    result = await publisher.publish_update(Task(id="t-publish-status"))

    assert result is False
    nc.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_nats_publisher_reports_success():
    """A successful NATS publish is observable at the publisher seam."""
    nc = MagicMock()
    nc.publish = AsyncMock()
    publisher = NatsPublisher(nc)

    result = await publisher.publish_update(Task(id="t-publish-ok"))

    assert result is True


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
        id="t1",
        description="d",
        project_id="p",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[st],
    )
    task.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    nw._orchestrator.tasks[task.id] = task

    assert nw._reset_subtask_to_pending("st-rejected") is True
    assert st.status == SubtaskStatus.PENDING
    assert st.assigned_worker is None
    assert task.updated_at > datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_reset_subtask_to_pending_skips_non_assigned():
    """Only ASSIGNED subtasks reset; a PENDING/COMPLETED one is untouched."""
    from ultimate_coders.agent.orchestrator import Orchestrator

    nw = _make_worker()
    nw._orchestrator = Orchestrator()

    st = Subtask(id="st-done", description="d", status=SubtaskStatus.COMPLETED)
    task = Task(
        id="t1",
        description="d",
        project_id="p",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[st],
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
    msg.data = json.dumps(
        {
            "task_id": "t-1",
            "subtask_id": "st-1",
            "description": "do the thing",
            "timeout_seconds": 600,
            "dispatch_mode": "prefer_remote",
            "steps": [],
        }
    ).encode()

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
    task.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
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
    assert task.updated_at > datetime(2020, 1, 1, tzinfo=timezone.utc)
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


@pytest.mark.asyncio
async def test_max_retry_failure_updates_task_timestamp():
    """A pending subtask at the retry limit must advance its parent snapshot."""
    from ultimate_coders.agent.orchestrator import Orchestrator

    nw = _make_worker()
    orch = Orchestrator()
    st = Subtask(
        id="st-max-retries",
        description="retry limit",
        parent_id="t-max-retries",
        status=SubtaskStatus.PENDING,
    )
    st.retry_count = orch.config.max_retries
    task = Task(
        id="t-max-retries",
        description="t",
        subtasks=[st],
        status=TaskStatus.IN_PROGRESS,
    )
    old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    task.updated_at = old_timestamp
    orch.tasks[task.id] = task
    nw._orchestrator = orch
    nw._worker = MagicMock()

    await nw._execute_subtasks(task)

    assert st.status == SubtaskStatus.FAILED
    assert task.status == TaskStatus.FAILED
    assert task.updated_at > old_timestamp


@pytest.mark.asyncio
async def test_local_worker_stall_updates_parent_task_timestamp():
    """Stall recovery must update the parent task along with subtask state."""
    from datetime import timedelta

    from ultimate_coders.agent.orchestrator import Orchestrator

    nw = _make_worker()
    orch = Orchestrator()
    st = Subtask(
        id="st-local-stall",
        description="local stall",
        parent_id="t-local-stall",
        status=SubtaskStatus.IN_PROGRESS,
    )
    st.assigned_worker = "local"
    task = Task(
        id="t-local-stall",
        description="t",
        subtasks=[st],
        status=TaskStatus.IN_PROGRESS,
    )
    old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    task.updated_at = old_timestamp
    orch.tasks[task.id] = task
    nw._orchestrator = orch
    nw._worker = MagicMock(
        worker_id="local",
        current_task=st,
        _last_heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    nw._dispatch_event = asyncio.Event()
    nw._running = True

    async def stop_after_tick(_seconds: float) -> None:
        nw._running = False

    with patch("ultimate_coders.nats_worker.asyncio.sleep", stop_after_tick):
        await nw._stale_worker_cleanup_loop()

    assert st.status == SubtaskStatus.PENDING
    assert st.assigned_worker is None
    assert st.retry_count == 1
    assert task.updated_at > old_timestamp
    assert nw._worker.current_task is None


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


@pytest.mark.asyncio
async def test_dashboard_worker_snapshot_includes_known_remote_workers():
    """The dashboard snapshot must include remote NATS dispatch workers.

    Remote workers intentionally stay out of ``Orchestrator.workers`` so the
    local scheduler does not treat them as direct execution targets. Their
    heartbeat state lives in ``_known_remote_workers`` and must be merged only
    when generating the monitoring snapshot.
    """
    from datetime import datetime, timezone

    from ultimate_coders.agent.orchestrator import Orchestrator
    from ultimate_coders.agent.types import WorkerInfo

    nw = _make_worker()
    orch = Orchestrator()
    await orch.register_worker(WorkerInfo(id="local", capabilities=["code"], max_capacity=3))
    nw._orchestrator = orch
    nw._known_remote_workers["remote"] = {
        "id": "remote",
        "capabilities": ["code"],
        "load": 1,
        "max_capacity": 3,
        "last_seen": datetime.now(timezone.utc),
    }

    snapshot = await nw._dash_listworkers({})

    assert {worker["id"] for worker in snapshot["workers"]} == {"local", "remote"}
    remote = next(worker for worker in snapshot["workers"] if worker["id"] == "remote")
    assert remote["current_load"] == 1
    assert remote["is_available"] is True
    assert snapshot["total"] == 2
    assert snapshot["available_count"] == 2


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


async def test_build_snapshot_empty_task_store_is_still_available():
    """An empty healthy task store must not be reported as disconnected."""
    nw = _make_worker()
    nw._dash_listworkers = AsyncMock(return_value={"available": True, "workers": []})
    nw._dash_getschedulerstatus = AsyncMock(return_value={"available": False})

    orch = MagicMock()
    orch.engine = None
    orch.tasks = {}
    orch._dashboard_app = None
    nw._orchestrator = orch

    snap = await nw._build_snapshot()

    assert snap["tasks"] == {
        "available": True,
        "tasks": [],
        "total": 0,
        "status_counts": {},
    }


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


# ── _handle_remote_subtask_result nesting ───────────────────────


async def test_handle_remote_subtask_result_reads_nested_payload():
    """Regression: _handle_remote_subtask_result read summary/modified_files/
    error from the top-level payload, but _make_task_event_payload nests them
    under payload["data"]. Remote file changes + errors were silently lost."""
    nw = _make_worker()
    nw._orchestrator = MagicMock()
    captured = []

    async def _capture(result):
        captured.append(result)

    nw._orchestrator.handle_subtask_result = _capture
    nw._orchestrator.get_task_status = MagicMock(return_value=None)

    payload = {
        "v": 1,
        "type": "subtask_completed",
        "task_id": "t1",
        "subtask_id": "st-1",
        "data": {
            "summary": "did the thing",
            "modified_files": [
                {"path": "src/a.py", "change_type": "modified", "diff_stats": "+1 -1"},
            ],
        },
    }
    await nw._handle_remote_subtask_result("subtask_completed", "t1", "st-1", payload)

    assert len(captured) == 1
    r = captured[0]
    assert r.success is True
    assert r.summary == "did the thing", "summary must come from nested data"
    assert len(r.modified_files) == 1, "modified_files must survive nesting"
    assert r.modified_files[0].file_path == "src/a.py"


async def test_handle_remote_subtask_result_publishes_parent_terminal_snapshot():
    """Remote result handling publishes the Orchestrator's full task state."""
    from ultimate_coders.agent.orchestrator import Orchestrator

    publisher = MagicMock()
    publisher.publish_update = AsyncMock()
    orch = Orchestrator(nats_publisher=publisher)
    task = Task(
        id="t-remote-terminal",
        description="d",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[Subtask(
            id="st-1",
            parent_id="t-remote-terminal",
            description="remote",
            status=SubtaskStatus.PENDING,
        )],
    )
    orch.tasks[task.id] = task

    nw = _make_worker()
    nw._orchestrator = orch
    nw._publisher = publisher
    payload = {
        "type": "subtask_completed",
        "task_id": task.id,
        "subtask_id": "st-1",
        "data": {"summary": "done"},
    }

    await nw._handle_remote_subtask_result("subtask_completed", task.id, "st-1", payload)

    publisher.publish_update.assert_awaited_once_with(task)
    assert task.status == TaskStatus.COMPLETED


async def test_handle_remote_subtask_result_reads_nested_error():
    """subtask_failed: error text must come from nested data, not fallback."""
    nw = _make_worker()
    nw._orchestrator = MagicMock()
    captured = []

    async def _capture(result):
        captured.append(result)

    nw._orchestrator.handle_subtask_result = _capture
    nw._orchestrator.get_task_status = MagicMock(return_value=None)

    payload = {
        "v": 1,
        "type": "subtask_failed",
        "task_id": "t1",
        "subtask_id": "st-1",
        "data": {"error": "boom: out of disk"},
    }
    await nw._handle_remote_subtask_result("subtask_failed", "t1", "st-1", payload)

    assert len(captured) == 1
    r = captured[0]
    assert r.success is False
    assert r.summary == "boom: out of disk", "error must come from nested data"


async def test_handle_remote_subtask_result_falls_back_when_data_missing():
    """No nested data key → fallback summary, no crash."""
    nw = _make_worker()
    nw._orchestrator = MagicMock()
    captured = []

    async def _capture(result):
        captured.append(result)

    nw._orchestrator.handle_subtask_result = _capture
    nw._orchestrator.get_task_status = MagicMock(return_value=None)

    payload = {"v": 1, "type": "subtask_failed", "task_id": "t1", "subtask_id": "st-1"}
    await nw._handle_remote_subtask_result("subtask_failed", "t1", "st-1", payload)

    assert captured[0].summary == "Remote subtask failed"


# ── _replay_missed_events skip-on-failure ───────────────────────


def _make_replay_msg(seq: int):
    """Build a fake JetStream msg with metadata.sequence.stream = seq."""
    msg = MagicMock()
    msg.metadata.sequence.stream = seq
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


async def test_replay_skips_malformed_event_and_advances_seq():
    """A malformed event that raises in _handle_task_event must not stall
    replay: ack it, advance _js_last_seq past it, log warning.

    Regression: old code swallowed the failure without acking/advancing, so
    the same broken event re-fetched and re-failed on every restart.
    """
    nw = _make_worker()
    nw._load_js_seq = MagicMock(return_value=5)
    saved: list[int] = []
    nw._save_js_seq = lambda s: saved.append(s)

    good_msg = _make_replay_msg(6)
    bad_msg = _make_replay_msg(7)
    acks = []

    async def _handle(msg):
        # First call (seq 6) succeeds; second (seq 7) simulates a malformed
        # event whose handler raises.
        if msg is bad_msg:
            raise ValueError("malformed event payload")

    nw._handle_task_event = _handle
    # Record ack order to assert the broken event was still acked.
    _orig_ack_good = good_msg.ack
    _orig_ack_bad = bad_msg.ack

    async def _ack_good():
        acks.append(6)
        await _orig_ack_good()

    async def _ack_bad():
        acks.append(7)
        await _orig_ack_bad()

    good_msg.ack = _ack_good
    bad_msg.ack = _ack_bad

    js = MagicMock()
    info = MagicMock()
    info.state.last_seq = 7
    js.stream_info = AsyncMock(return_value=info)
    sub = MagicMock()
    sub.fetch = AsyncMock(return_value=[good_msg, bad_msg])
    sub.unsubscribe = AsyncMock()
    js.pull_subscribe = AsyncMock(return_value=sub)
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    await nw._replay_missed_events()

    # Both messages acked, including the one whose handler raised.
    assert acks == [6, 7]
    # Seq advanced past the broken event (not stuck at 6).
    assert nw._js_last_seq == 7
    # Persisted seq reflects progress past the broken event.
    assert saved[-1] == 7


# ── _spawn_bg done-callback logs exceptions ─────────────────────


def test_spawn_bg_logs_failed_task_exception(caplog: pytest.LogCaptureFixture) -> None:
    """A bg task whose coro raises must surface a warning (with traceback), not
    die silently as asyncio's default 'Task exception was never retrieved'.

    Regression: old done-callback only discarded the strong ref.
    """

    async def _run() -> None:
        nw = _make_worker()

        async def _boom() -> None:
            raise ValueError("dispatch exploded")

        caplog.set_level(logging.WARNING, logger="ultimate_coders.nats_worker")
        task = nw._spawn_bg(_boom())
        try:
            await task
        except ValueError:
            pass
        # Let the done-callback run.
        await asyncio.sleep(0)
        return task, nw

    task, nw = asyncio.run(_run())

    # Strong ref released.
    assert task not in nw._bg_tasks
    # Warning logged with the exception.
    assert any("Background task failed" in r.message and r.exc_info for r in caplog.records)


def test_spawn_bg_cancelled_task_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A cancelled bg task is expected (shutdown) — must NOT log a warning."""

    async def _run() -> None:
        nw = _make_worker()

        async def _hang() -> None:
            await asyncio.sleep(60)

        caplog.set_level(logging.WARNING, logger="ultimate_coders.nats_worker")
        task = nw._spawn_bg(_hang())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert not any("Background task failed" in r.message for r in caplog.records)


def test_event_payload_message_id_distinct_for_rapid_same_type_events():
    """Regression: a 5s bucket made two same-type events for one subtask
    within 5s share a message_id → Rust-side dedup (5min TTL) dropped the
    second, losing real state transitions (e.g. two subtask_progress phase
    changes). message_id must now use ms precision so distinct events differ.
    """
    p1 = _make_task_event_payload("subtask_progress", "t1", "st-1", {"percent": 10})
    # Spin until the ms clock advances (usually 1 iteration), then build p2.
    import time

    t0 = int(time.time() * 1000)
    while int(time.time() * 1000) == t0:
        time.sleep(0.001)
    p2 = _make_task_event_payload("subtask_progress", "t1", "st-1", {"percent": 50})
    assert p1["message_id"] != p2["message_id"], (
        "same-type events must get distinct message_ids (ms precision), "
        "else Rust dedup drops the second"
    )


def test_event_payload_message_id_distinct_within_same_millisecond():
    """A sequence suffix prevents collisions when two events share one ms."""
    with patch("time.time", return_value=123.456789):
        p1 = _make_task_event_payload("subtask_progress", "t1", "st-1", {"percent": 10})
        p2 = _make_task_event_payload("subtask_progress", "t1", "st-1", {"percent": 50})

    assert p1["message_id"] != p2["message_id"]
    assert p1["message_id"].rsplit(":", 1)[1] == "123456"
    assert p2["message_id"].rsplit(":", 1)[1] == "123456"


def test_event_payload_message_id_no_5s_bucket_suffix():
    """The old format ended in a 5s-bucket integer (ts_ms // 5000). The new
    format uses ms precision so the suffix must be the raw ms timestamp,
    not a bucketed value."""
    import time

    before = int(time.time() * 1000)
    p = _make_task_event_payload("subtask_started", "t1", "st-1")
    after = int(time.time() * 1000)
    suffix = int(p["message_id"].rsplit(":", 1)[1])
    assert before <= suffix <= after, "message_id suffix should be raw ms, not a bucket"


def test_registration_metadata_shape(monkeypatch):
    """Cross-host observability (#607 follow-up): the gateway registration
    metadata must be stable JSON with hostname+pid always present and
    compose_project only when running under a compose project."""
    from ultimate_coders.nats_worker import NatsWorker

    monkeypatch.delenv("UC_COMPOSE_PROJECT", raising=False)
    meta = json.loads(NatsWorker._registration_metadata())
    assert isinstance(meta["hostname"], str) and meta["hostname"]
    assert isinstance(meta["pid"], int)
    assert "compose_project" not in meta

    monkeypatch.setenv("UC_COMPOSE_PROJECT", "uc-prod")
    meta = json.loads(NatsWorker._registration_metadata())
    assert meta["compose_project"] == "uc-prod"
