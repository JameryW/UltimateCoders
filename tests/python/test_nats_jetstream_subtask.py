"""Tests for JetStream subtask delivery (PR1: stream + consumer + ACK refactor).

Covers:
- UC_SUBTASKS stream + subtask-workers consumer setup (non-fatal on failure)
- _start_subtask_consumer: JS available → fetch loop; JS unavailable → fallback
- _handle_subtask_execute_js: max_deliver cap → term + subtask_failed
- _handle_subtask_execute_js: capability miss → nak (redeliver to another worker)
- _execute_and_report: ACK-after-execution (success → ack, failure → ack)
- Fallback: JS unavailable → core NATS queue group subscribe
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.agent.types import Subtask
from ultimate_coders.nats_worker import NatsWorker as _NatsWorker


def _make_worker() -> _NatsWorker:
    """Build a NatsWorker without running start() (no NATS/IO)."""
    return _NatsWorker(project_path="/tmp/test", mode="worker")


def _make_js_msg(
    data: dict,
    num_delivered: int = 1,
    ack_mock: AsyncMock | None = None,
    nak_mock: AsyncMock | None = None,
    term_mock: AsyncMock | None = None,
) -> MagicMock:
    """Build a fake JetStream msg with metadata.num_delivered."""
    msg = MagicMock()
    msg.data = json.dumps(data).encode()
    msg.metadata.num_delivered = num_delivered
    msg.metadata.sequence.stream = 1
    msg.ack = ack_mock or AsyncMock()
    msg.nak = nak_mock or AsyncMock()
    msg.term = term_mock or AsyncMock()
    return msg


def _make_subtask_payload(
    task_id: str = "t-1",
    subtask_id: str = "st-1",
    description: str = "do the thing",
    required_capabilities: list[str] | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "subtask_id": subtask_id,
        "description": description,
        "timeout_seconds": 600,
        "dispatch_mode": "prefer_remote",
        "steps": [],
        "required_capabilities": required_capabilities or [],
    }


# ── Stream / consumer setup ─────────────────────────────────────


async def test_ensure_subtask_stream_creates_workqueue_stream():
    """_ensure_jetstream_subtask_stream calls add_stream with workqueue retention."""
    nw = _make_worker()

    js = MagicMock()
    js.add_stream = AsyncMock()
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    await nw._ensure_jetstream_subtask_stream()

    js.add_stream.assert_awaited_once()
    call_kwargs = js.add_stream.call_args.kwargs
    assert call_kwargs["name"] == "UC_SUBTASKS"
    assert call_kwargs["subjects"] == ["uc.subtask.execute"]
    assert call_kwargs["retention"] == "workqueue"
    assert call_kwargs["max_age"] == 7 * 24 * 3600
    assert call_kwargs["duplicate_window"] == 120


async def test_ensure_subtask_stream_already_exists_is_non_fatal():
    """Stream already exists → debug log, no crash."""
    nw = _make_worker()

    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=Exception("stream already exists"))
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    # Must not raise
    await nw._ensure_jetstream_subtask_stream()


async def test_ensure_subtask_stream_js_unavailable_is_non_fatal():
    """JetStream unavailable → warning, no crash."""
    nw = _make_worker()

    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=Exception("JetStream unavailable"))
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    # Must not raise
    await nw._ensure_jetstream_subtask_stream()


async def test_ensure_subtask_consumer_creates_durable():
    """_ensure_jetstream_subtask_consumer creates subtask-workers with max_deliver=5."""
    nw = _make_worker()

    js = MagicMock()
    js.add_consumer = AsyncMock()
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    await nw._ensure_jetstream_subtask_consumer()

    js.add_consumer.assert_awaited_once()
    call_kwargs = js.add_consumer.call_args.kwargs
    assert call_kwargs["stream"] == "UC_SUBTASKS"
    assert call_kwargs["durable_name"] == "subtask-workers"
    assert call_kwargs["ack_policy"] == "explicit"
    assert call_kwargs["max_deliver"] == 5


async def test_ensure_subtask_consumer_already_exists_is_non_fatal():
    """Consumer already exists → debug log, no crash."""
    nw = _make_worker()

    js = MagicMock()
    js.add_consumer = AsyncMock(
        side_effect=Exception("consumer already exists")
    )
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    # Must not raise
    await nw._ensure_jetstream_subtask_consumer()


# ── _start_subtask_consumer: JS available vs fallback ───────────


async def test_start_subtask_consumer_js_available_starts_fetch_loop():
    """JS available → pull_subscribe + fetch loop task started, returns True."""
    nw = _make_worker()
    nw._running = True

    pull_sub = MagicMock()
    js = MagicMock()
    js.pull_subscribe = AsyncMock(return_value=pull_sub)
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    worker = MagicMock()
    worker.max_capacity = 3
    nw._worker = worker

    result = await nw._start_subtask_consumer()

    assert result is True
    assert nw._subtask_js_available is True
    assert nw._subtask_pull_sub is pull_sub
    assert nw._subtask_fetch_task is not None

    # Cleanup
    nw._subtask_fetch_task.cancel()
    try:
        await nw._subtask_fetch_task
    except asyncio.CancelledError:
        pass


async def test_start_subtask_consumer_js_unavailable_returns_false():
    """JS unavailable → pull_subscribe raises, returns False (fallback signal)."""
    nw = _make_worker()

    js = MagicMock()
    js.pull_subscribe = AsyncMock(side_effect=Exception("JetStream unavailable"))
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)

    result = await nw._start_subtask_consumer()

    assert result is False
    assert nw._subtask_js_available is False
    assert nw._subtask_pull_sub is None
    assert nw._subtask_fetch_task is None


# ── _handle_subtask_execute_js: max_deliver cap ─────────────────


async def test_js_max_deliver_cap_terms_and_publishes_failed():
    """num_delivered >= max_deliver → term() + subtask_failed event (no execution)."""
    nw = _make_worker()
    nw._running = True

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.capabilities = []
    worker.execute_subtask = AsyncMock()  # must NOT be called
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    nak = AsyncMock()
    term = AsyncMock()
    msg = _make_js_msg(
        _make_subtask_payload(),
        num_delivered=5,  # at the cap
        ack_mock=ack,
        nak_mock=nak,
        term_mock=term,
    )

    await nw._handle_subtask_execute_js(msg)

    # Term-acked (stop redelivery)
    term.assert_awaited_once()
    ack.assert_not_awaited()
    nak.assert_not_awaited()

    # subtask_failed published with max_delivered flag
    publisher.publish_event.assert_awaited_once()
    call_args = publisher.publish_event.call_args
    assert call_args.args[0] == "subtask_failed"
    assert call_args.kwargs["data"]["max_delivered"] is True

    # Execution was NOT started (poison subtask)
    worker.execute_subtask.assert_not_awaited()


async def test_js_below_max_deliver_executes_normally():
    """num_delivered < max_deliver → normal execution path (not term-acked early)."""
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

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    msg = _make_js_msg(
        _make_subtask_payload(),
        num_delivered=2,  # below cap
        ack_mock=ack,
    )

    # _handle_subtask_execute_js spawns _execute_and_report as bg task
    await nw._handle_subtask_execute_js(msg)

    # Wait for execution to start
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set(), "execution never started"

    # ACK must NOT have happened yet (ack-after-execution)
    ack.assert_not_awaited()

    # Let execution complete
    release.set()
    for _ in range(50):
        if ack.await_count >= 1:
            break
        await asyncio.sleep(0.02)

    # ACK happened AFTER execution completed
    ack.assert_awaited_once()


# ── _handle_subtask_execute_js: capability miss → nak ───────────


async def test_js_capability_miss_naks_and_publishes_rejection():
    """Capability miss → nak() (redeliver to another worker) + rejection event."""
    nw = _make_worker()
    nw._running = True

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.capabilities = ["python"]  # missing "rust"
    worker.execute_subtask = AsyncMock()  # must NOT be called
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    nak = AsyncMock()
    term = AsyncMock()
    msg = _make_js_msg(
        _make_subtask_payload(required_capabilities=["rust"]),
        num_delivered=1,
        ack_mock=ack,
        nak_mock=nak,
        term_mock=term,
    )

    await nw._handle_subtask_execute_js(msg)

    # nak'd (redeliver to another worker)
    nak.assert_awaited_once()
    ack.assert_not_awaited()
    term.assert_not_awaited()

    # Rejection event published
    publisher.publish_event.assert_awaited_once()
    call_args = publisher.publish_event.call_args
    assert call_args.args[0] == "subtask_dispatch_rejected"

    # Execution was NOT started
    worker.execute_subtask.assert_not_awaited()


# ── _execute_and_report: ACK-after-execution ────────────────────


async def test_execute_and_report_acks_on_success():
    """Successful execution → ack after publish (ack-after-execution)."""
    nw = _make_worker()
    nw._running = True

    async def execute(subtask):
        result = MagicMock()
        result.success = True
        result.summary = "done"
        result.modified_files = []
        return result

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.execute_subtask = execute
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    js_msg = MagicMock()
    js_msg.ack = ack

    subtask = Subtask(id="st-1", parent_id="t-1", description="d")
    await nw._execute_and_report(subtask, js_msg=js_msg)

    # subtask_completed published
    publisher.publish_event.assert_awaited_once()
    assert publisher.publish_event.call_args.args[0] == "subtask_completed"

    # ACK happened after execution
    ack.assert_awaited_once()


async def test_execute_and_report_acks_on_failure():
    """Failed execution → still ack (failure reported via event, no re-run)."""
    nw = _make_worker()
    nw._running = True

    async def execute(subtask):
        result = MagicMock()
        result.success = False
        result.summary = "failed"
        result.error = "boom"
        result.modified_files = []
        return result

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.execute_subtask = execute
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    js_msg = MagicMock()
    js_msg.ack = ack

    subtask = Subtask(id="st-1", parent_id="t-1", description="d")
    await nw._execute_and_report(subtask, js_msg=js_msg)

    # subtask_failed published
    publisher.publish_event.assert_awaited_once()
    assert publisher.publish_event.call_args.args[0] == "subtask_failed"

    # ACK happened (don't redeliver a failing subtask)
    ack.assert_awaited_once()


async def test_execute_and_report_acks_on_exception():
    """Execution raises → still ack (exception reported via subtask_failed)."""
    nw = _make_worker()
    nw._running = True

    async def execute(subtask):
        raise RuntimeError("sandbox crashed")

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.execute_subtask = execute
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    js_msg = MagicMock()
    js_msg.ack = ack

    subtask = Subtask(id="st-1", parent_id="t-1", description="d")
    await nw._execute_and_report(subtask, js_msg=js_msg)

    # subtask_failed published with the exception message
    publisher.publish_event.assert_awaited_once()
    assert publisher.publish_event.call_args.args[0] == "subtask_failed"
    assert "sandbox crashed" in publisher.publish_event.call_args.kwargs["data"]["error"]

    # ACK happened
    ack.assert_awaited_once()


async def test_execute_and_report_acks_on_publish_failure():
    """Publish raises after successful execution → still ack (finally block).

    This is the load-bearing ACK-timing edge case: execution succeeded but
    publish_event/publish_update raised. The JS msg must still be acked —
    the subtask already ran, redelivering would re-execute it. The ack is
    in a finally block so it fires regardless of publish outcome.
    """
    nw = _make_worker()
    nw._running = True

    async def execute(subtask):
        result = MagicMock()
        result.success = True
        result.summary = "done"
        result.modified_files = []
        return result

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.execute_subtask = execute
    nw._worker = worker

    publisher = MagicMock()
    # publish_event raises — simulates NATS connection drop mid-publish
    publisher.publish_event = AsyncMock(side_effect=ConnectionError("NATS down"))
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    js_msg = MagicMock()
    js_msg.ack = ack

    subtask = Subtask(id="st-1", parent_id="t-1", description="d")
    # Must not raise — the publish exception is caught
    await nw._execute_and_report(subtask, js_msg=js_msg)

    # ACK happened despite publish failure (finally block)
    ack.assert_awaited_once()


async def test_execute_and_report_acks_on_publish_update_failure():
    """publish_update raises → still ack (finally block covers all publish paths)."""
    nw = _make_worker()
    nw._running = True

    async def execute(subtask):
        result = MagicMock()
        result.success = True
        result.summary = "done"
        result.modified_files = []
        return result

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.execute_subtask = execute
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    # publish_update raises — second publish call fails
    publisher.publish_update = AsyncMock(side_effect=RuntimeError("gRPC down"))
    nw._publisher = publisher

    ack = AsyncMock()
    js_msg = MagicMock()
    js_msg.ack = ack

    subtask = Subtask(id="st-1", parent_id="t-1", description="d")
    await nw._execute_and_report(subtask, js_msg=js_msg)

    # ACK happened despite publish_update failure (finally block)
    ack.assert_awaited_once()


async def test_execute_and_report_no_js_msg_no_ack():
    """Core NATS path (js_msg=None) → no ack attempt (core NATS has no ack)."""
    nw = _make_worker()
    nw._running = True

    async def execute(subtask):
        result = MagicMock()
        result.success = True
        result.summary = "done"
        result.modified_files = []
        return result

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.execute_subtask = execute
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    subtask = Subtask(id="st-1", parent_id="t-1", description="d")
    # No js_msg — must not crash
    await nw._execute_and_report(subtask, js_msg=None)

    publisher.publish_event.assert_awaited_once()


async def test_cancelled_task_skips_execution_and_acks_jetstream():
    """A task cancelled before pickup must never start its sandbox process."""
    nw = _make_worker()
    nw._cancelled_task_ids.add("t-1")

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.execute_subtask = AsyncMock()
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    ack = AsyncMock()
    js_msg = MagicMock()
    js_msg.ack = ack

    await nw._execute_and_report(
        Subtask(id="st-1", parent_id="t-1", description="do not run"),
        js_msg=js_msg,
    )

    worker.execute_subtask.assert_not_awaited()
    publisher.publish_event.assert_not_awaited()
    publisher.publish_update.assert_not_awaited()
    ack.assert_awaited_once()


async def test_cancel_event_stops_running_remote_execution():
    """A task_cancelled event stops a remote execution already in progress."""
    nw = _make_worker()
    nw._dispatch_event = asyncio.Event()

    orchestrator = MagicMock()
    orchestrator.cancel_task = AsyncMock(return_value=True)
    nw._orchestrator = orchestrator

    async def wait_forever():
        await asyncio.Event().wait()

    execution = asyncio.create_task(wait_forever())
    nw._running_subtask_tasks["t-1"] = {execution}

    msg = MagicMock()
    msg.data = json.dumps({"type": "task_cancelled", "task_id": "t-1"}).encode()
    await nw._handle_task_event(msg)
    await asyncio.sleep(0)

    assert "t-1" in nw._cancelled_task_ids
    assert execution.cancelled()
    orchestrator.cancel_task.assert_awaited_once_with("t-1")
    assert nw._dispatch_event.is_set()


# ── Core NATS fallback path still works ─────────────────────────


async def test_handle_subtask_execute_core_nats_still_works():
    """The core NATS fallback path (_handle_subtask_execute) still dispatches."""
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

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    msg = MagicMock()
    msg.data = json.dumps(_make_subtask_payload()).encode()

    await nw._handle_subtask_execute(msg)

    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set()

    release.set()
    for _ in range(50):
        if publisher.publish_event.await_count >= 1:
            break
        await asyncio.sleep(0.02)
    assert publisher.publish_event.await_count >= 1


async def test_core_nats_capability_miss_publishes_rejection_no_nak():
    """Core NATS capability miss → rejection event, no nak (core NATS has no nak)."""
    nw = _make_worker()
    nw._running = True

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.capabilities = ["python"]
    worker.execute_subtask = AsyncMock()
    nw._worker = worker

    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    nw._publisher = publisher

    msg = MagicMock()
    msg.data = json.dumps(
        _make_subtask_payload(required_capabilities=["rust"])
    ).encode()

    await nw._handle_subtask_execute(msg)

    # The rejection is published via _spawn_bg(_publish_capability_rejection),
    # so wait for the bg task to complete.
    for _ in range(50):
        if publisher.publish_event.await_count >= 1:
            break
        await asyncio.sleep(0.02)

    # Rejection event published
    publisher.publish_event.assert_awaited_once()
    assert publisher.publish_event.call_args.args[0] == "subtask_dispatch_rejected"

    # Execution NOT started
    worker.execute_subtask.assert_not_awaited()


# ── Malformed message handling ──────────────────────────────────


async def test_js_malformed_message_terminates():
    """Malformed JS message → term() (stop redelivery of unparseable message)."""
    nw = _make_worker()
    nw._running = True

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.capabilities = []
    nw._worker = worker

    ack = AsyncMock()
    term = AsyncMock()
    msg = _make_js_msg({}, num_delivered=1, ack_mock=ack, term_mock=term)
    msg.data = b"not valid json"

    await nw._handle_subtask_execute_js(msg)

    term.assert_awaited_once()
    ack.assert_not_awaited()


async def test_js_missing_ids_terminates():
    """JS message missing task_id/subtask_id → term()."""
    nw = _make_worker()
    nw._running = True

    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.capabilities = []
    nw._worker = worker

    ack = AsyncMock()
    term = AsyncMock()
    msg = _make_js_msg({}, num_delivered=1, ack_mock=ack, term_mock=term)
    msg.data = json.dumps({"task_id": "", "subtask_id": ""}).encode()

    await nw._handle_subtask_execute_js(msg)

    term.assert_awaited_once()
