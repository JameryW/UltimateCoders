"""Tests for JetStream subtask delivery.

Covers:
- _ensure_subtask_transport: JetStream is a hard dependency (T5 #641 /
  D4 #633 Q1) — the durable pull consumer is bound with periodic retries,
  gateway registration is refused until it is usable, and there is no
  core-NATS fallback
- _handle_subtask_execute_js: max_deliver cap → term + subtask_failed
- _handle_subtask_execute_js: capability miss → nak (redeliver to another worker)
- _execute_and_report: ACK-after-execution (success → ack, failure → ack)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

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
    # T4 #640 (D3 lockstep): the wire no longer carries task_id/subtask_id —
    # the execution envelope is the single identity source. Identity mapping
    # matches the Rust publishers: graph_id = task_id, node_id = subtask_id,
    # attempt_id = dispatch retry counter (0 for a fresh dispatch).
    return {
        "description": description,
        "timeout_seconds": 600,
        "dispatch_mode": "prefer_remote",
        "steps": [],
        "required_capabilities": required_capabilities or [],
        "graph_id": task_id,
        "node_id": subtask_id,
        "attempt_id": 0,
        "idempotency_key": f"{task_id}:{subtask_id}:0",
        "worker_epoch": "",
        "contract_version": "v1",
    }


async def test_original_request_survives_remote_dispatch():
    sender = _NatsWorker(project_path="/tmp/test", mode="default")
    sender._orchestrator = MagicMock()
    sender._orchestrator.assign_subtask = AsyncMock(return_value="remote")
    sender._publisher = MagicMock()
    sender._nc = MagicMock()
    sender._nc.publish = AsyncMock()
    original = "Read README.md and return only its first Markdown heading."
    subtask = Subtask(
        id="t-original-s0",
        parent_id="t-original",
        description="Read README.md",
        user_request=original,
    )

    await sender._dispatch_remote(subtask)

    payload = json.loads(sender._nc.publish.await_args.args[1])
    receiver = _make_worker()
    receiver._worker = MagicMock()
    restored = receiver._build_subtask_from_data(
        "t-original", "t-original-s0", payload,
    )
    assert restored is not None
    assert restored.user_request == original


# ── _ensure_subtask_transport: hard dependency, no fallback (T5 #641) ──


async def test_ensure_subtask_transport_binds_and_starts_fetch_loop():
    """JetStream usable → durable consumer asserted, pull sub bound, fetch
    loop started, transport flagged available, registration triggered.

    T12 #654: the shared (overflow) consumer is joined by this worker's
    PER-WORKER consumer, which is what makes the worker targetable by
    affinity placement. Two add_consumer + two pull_subscribe calls.
    """
    nw = _make_worker()
    nw._running = True

    pull_sub = MagicMock()
    per_worker_pull_sub = MagicMock()
    js = MagicMock()
    js.add_consumer = AsyncMock()
    js.pull_subscribe = AsyncMock(side_effect=[pull_sub, per_worker_pull_sub])
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)
    nw._register_with_gateway = AsyncMock()

    worker = MagicMock()
    worker.max_capacity = 3
    worker.get_info = MagicMock(return_value=MagicMock(id="w-1"))
    nw._worker = worker

    result = await nw._ensure_subtask_transport()

    assert result is True
    assert nw._subtask_js_available is True
    assert nw._subtask_pull_sub is pull_sub
    assert nw._subtask_fetch_task is not None
    # Per-worker side (T12 #654).
    assert nw._per_worker_topic is True
    assert nw._per_worker_pull_sub is per_worker_pull_sub
    assert nw._per_worker_fetch_task is not None

    # D4 Q1: the durable consumer is asserted explicitly (ack policy +
    # max_deliver poison guard live server-side) before the pull
    # subscription binds. Shared (overflow) first, then per-worker.
    assert js.add_consumer.await_count == 2
    shared_kwargs = js.add_consumer.await_args_list[0].kwargs
    assert shared_kwargs["stream"] == "UC_SUBTASKS"
    assert shared_kwargs["durable_name"] == "subtask-workers"
    assert shared_kwargs["ack_policy"] == "explicit"
    assert shared_kwargs["max_deliver"] == 5
    per_worker_kwargs = js.add_consumer.await_args_list[1].kwargs
    assert per_worker_kwargs["stream"] == "UC_SUBTASKS"
    assert per_worker_kwargs["durable_name"] == "subtask-worker-w-1"
    assert per_worker_kwargs["filter_subject"] == "uc.subtask.execute.w.w-1"
    assert per_worker_kwargs["ack_policy"] == "explicit"
    assert per_worker_kwargs["max_deliver"] == 5

    assert js.pull_subscribe.await_count == 2
    # Transport became usable → (re-)register with the gateway immediately
    # instead of waiting for the next heartbeat tick.
    nw._register_with_gateway.assert_awaited_once()

    # Cleanup
    for task in (nw._subtask_fetch_task, nw._per_worker_fetch_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_ensure_subtask_transport_retries_while_js_unavailable():
    """JetStream unavailable → stays unbound, retries periodically, never
    flips _subtask_js_available (a worker that cannot consume dispatches
    must not look dispatchable). The test double raises exactly like the
    real JS client does when the stream/consumer is missing."""
    nw = _make_worker()
    nw._SUBTASK_TRANSPORT_RETRY_SECONDS = 0.01  # test-speed retry cadence

    js = MagicMock()
    js.add_consumer = AsyncMock(side_effect=Exception("JetStream unavailable"))
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)
    nw._register_with_gateway = AsyncMock()

    bind_task = asyncio.create_task(nw._ensure_subtask_transport())
    try:
        for _ in range(50):
            if js.add_consumer.await_count >= 3:
                break
            await asyncio.sleep(0.02)
        assert js.add_consumer.await_count >= 3, "transport never retried"

        assert nw._subtask_js_available is False
        assert nw._subtask_pull_sub is None
        assert nw._subtask_fetch_task is None
        # T12 #654: nothing per-worker either — the shared bind never
        # succeeded, so the additive bind was never reached.
        assert nw._per_worker_topic is False
        assert nw._per_worker_fetch_task is None
        # Refused registration: never attempted while the transport is down.
        nw._register_with_gateway.assert_not_awaited()
    finally:
        bind_task.cancel()
        try:
            await bind_task
        except asyncio.CancelledError:
            pass


async def test_register_with_gateway_refused_while_transport_down():
    """D4 Q1: a worker-mode worker without a usable subtask transport must
    not register with the gateway — it would receive dispatches it cannot
    consume (the core-NATS fallback is gone)."""
    nw = _make_worker()  # mode="worker"
    nw._subtask_js_available = False
    nw._grpc_endpoint = "http://127.0.0.1:50051"

    with patch("ultimate_coders.nats_worker.Engine") as engine_cls:
        await nw._register_with_gateway()
        engine_cls.assert_not_called()

    assert nw._grpc_reg_engine is None


async def test_registration_metadata_reports_subtask_transport():
    """Registration metadata carries subtask_transport (D4 Q1 observability)."""
    nw = _make_worker()

    nw._subtask_js_available = False
    meta = json.loads(nw._registration_metadata())
    assert meta["subtask_transport"] == "unavailable"

    nw._subtask_js_available = True
    meta = json.loads(nw._registration_metadata())
    assert meta["subtask_transport"] == "jetstream"


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
    publisher.publish_update.assert_awaited_once()
    assert publisher.publish_update.call_args.kwargs["partial"] is True

    # ACK happened after execution
    ack.assert_awaited_once()


async def test_long_running_subtask_renews_jetstream_ack_wait():
    """An agent run beyond ack_wait must keep its lease until the final ACK."""
    nw = _make_worker()
    nw._SUBTASK_PROGRESS_INTERVAL_SECONDS = 0.01
    finish = asyncio.Event()

    async def execute(subtask):
        await finish.wait()
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

    js_msg = MagicMock()
    js_msg.in_progress = AsyncMock()
    js_msg.ack = AsyncMock()
    running = asyncio.create_task(
        nw._execute_and_report(Subtask(id="st-1", parent_id="t-1", description="d"), js_msg=js_msg)
    )
    await asyncio.sleep(0.035)
    assert js_msg.in_progress.await_count >= 2
    js_msg.ack.assert_not_awaited()

    finish.set()
    await running
    js_msg.ack.assert_awaited_once()
    renewals = js_msg.in_progress.await_count
    await asyncio.sleep(0.025)
    assert js_msg.in_progress.await_count == renewals


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
    """js_msg=None → no ack attempt (defensive: nothing to ack)."""
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
    """JS message without a full execution envelope → term() (T4 #640 / D7)."""
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
