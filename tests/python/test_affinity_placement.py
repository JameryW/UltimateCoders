"""Tests for affinity placement (T12 #654 / D12 #649).

Covers the worker half of the placement contract:

- per-worker subject / durable naming (cross-language golden shared with
  ``uc_grpc::placement``)
- ``_bind_per_worker_consumer``: success declares ``per_worker_topic``;
  failure is best-effort and leaves the worker legacy (overflow only)
- ``_ensure_subtask_transport`` binds BOTH consumers, and stays ready when
  only the shared one binds
- heartbeat carries ``recent_files`` + ``per_worker_topic`` (worker mode)
  and neutral signals otherwise
- ``Engine.worker_heartbeat_async`` forwards the two new arguments
- ``Worker.record_recent_files``: bounded, de-duplicated, move-to-front
- ``_execute_in_sandbox`` records declared ∪ changed files
- ``stop()`` tears the per-worker loop down

Placement is a SOFT preference: nothing here may ever make a node
undeliverable. The shared subject remains the overflow path, so a worker
that fails to bind its per-worker consumer is simply never targeted.
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import FileChange, Subtask
from ultimate_coders.agent.worker import Worker
from ultimate_coders.engine import Engine
from ultimate_coders.nats_worker import CONTRACT_VERSION
from ultimate_coders.nats_worker import NatsWorker as _NatsWorker


def _make_worker(mode: str = "worker") -> _NatsWorker:
    """Build a NatsWorker without running start() (no NATS/IO)."""
    return _NatsWorker(project_path="/tmp/test", mode=mode)


def _make_js(pull_subs: list[MagicMock] | None = None) -> MagicMock:
    js = MagicMock()
    js.add_consumer = AsyncMock()
    js.pull_subscribe = AsyncMock(
        side_effect=pull_subs if pull_subs is not None else [MagicMock()]
    )
    return js


# ── Subject / durable naming (cross-language contract) ───────────


def test_per_worker_subject_matches_the_rust_golden():
    """The gateway (Rust) builds this exact string to publish, the worker to
    bind. A drift here would silently send every targeted node into a subject
    nobody reads — pinned on both sides."""
    assert _NatsWorker._per_worker_subject("worker-7") == "uc.subtask.execute.w.worker-7"
    assert (
        _NatsWorker._SUBTASK_PER_WORKER_SUBJECT_PREFIX == "uc.subtask.execute.w."
    )
    # The wildcard is a strict EXTENSION: NATS `>` needs one more token, so
    # `.w.>` never captures the shared subject. The gateway stream lists both.
    assert (
        _NatsWorker._per_worker_subject("w1")
        == "uc.subtask.execute" + ".w.w1"
    )


def test_per_worker_durable_is_stable_and_sanitized():
    """Durable names reject `.`, `*` and `>`; the mapping must stay
    deterministic (it is re-derived on every reconnect) and collision-free."""
    assert _NatsWorker._per_worker_durable("worker-7") == "subtask-worker-worker-7"
    # Re-derivation is stable.
    assert _NatsWorker._per_worker_durable("worker-7") == _NatsWorker._per_worker_durable(
        "worker-7"
    )

    dotted = _NatsWorker._per_worker_durable("pod.a")
    underscored = _NatsWorker._per_worker_durable("pod_a")
    # Same sanitized stem, but different ids must not collide.
    assert dotted != underscored
    assert dotted.startswith("subtask-worker-pod_a-")
    assert hashlib.sha256(b"pod.a").hexdigest()[:8] in dotted

    # Deeper oddities are fully sanitized (no dot can survive).
    weird = _NatsWorker._per_worker_durable("a.b>c*d e")
    assert "." not in weird.replace("subtask-worker-", "")
    assert ">" not in weird and "*" not in weird and " " not in weird

    # An id made entirely of invalid characters still yields a usable name.
    assert _NatsWorker._per_worker_durable("").startswith("subtask-worker-w-")


def test_transport_worker_id_matches_registration_identity():
    """The per-worker subject must be keyed by the id the gateway knows, or a
    targeted publish would land on a subject this worker never bound."""
    nw = _make_worker()
    # No worker object yet (pre-start) → fall back to the consumer id.
    assert nw._transport_worker_id() == nw._consumer_id

    worker = MagicMock()
    worker.get_info = MagicMock(return_value=MagicMock(id="w-1"))
    nw._worker = worker
    assert nw._transport_worker_id() == "w-1"


# ── _bind_per_worker_consumer ────────────────────────────────────


async def test_bind_per_worker_consumer_declares_and_starts_loop():
    nw = _make_worker()
    nw._running = True
    worker = MagicMock()
    worker.max_capacity = 2
    worker.get_info = MagicMock(return_value=MagicMock(id="w-1"))
    nw._worker = worker

    per_worker_sub = MagicMock()
    js = _make_js([per_worker_sub])

    ok = await nw._bind_per_worker_consumer(js)

    assert ok is True
    assert nw._per_worker_topic is True
    assert nw._per_worker_pull_sub is per_worker_sub
    assert nw._per_worker_fetch_task is not None

    kwargs = js.add_consumer.await_args.kwargs
    assert kwargs["stream"] == "UC_SUBTASKS"
    assert kwargs["durable_name"] == "subtask-worker-w-1"
    assert kwargs["filter_subject"] == "uc.subtask.execute.w.w-1"
    assert kwargs["ack_policy"] == "explicit"
    assert kwargs["max_deliver"] == 5

    sub_args, sub_kwargs = js.pull_subscribe.await_args
    assert sub_args[0] == "uc.subtask.execute.w.w-1"
    assert sub_kwargs["durable"] == "subtask-worker-w-1"
    assert sub_kwargs["stream"] == "UC_SUBTASKS"

    nw._per_worker_fetch_task.cancel()
    try:
        await nw._per_worker_fetch_task
    except asyncio.CancelledError:
        pass


async def test_bind_per_worker_consumer_failure_is_best_effort(caplog):
    """A per-worker bind failure must NOT fail the worker — it stays legacy
    (never targeted, still served by the shared overflow consumer)."""
    nw = _make_worker()
    nw._running = True
    nw._worker = MagicMock()
    nw._worker.get_info = MagicMock(return_value=MagicMock(id="w-1"))

    js = MagicMock()
    js.add_consumer = AsyncMock(side_effect=Exception("stream has no such subject"))
    js.pull_subscribe = AsyncMock()

    ok = await nw._bind_per_worker_consumer(js)

    assert ok is False
    assert nw._per_worker_topic is False
    assert nw._per_worker_pull_sub is None
    assert nw._per_worker_fetch_task is None
    js.pull_subscribe.assert_not_awaited()
    assert any("Per-worker subtask consumer unavailable" in r.message for r in caplog.records)


async def test_bind_per_worker_consumer_swallows_pull_subscribe_failure():
    """add_consumer may succeed and pull_subscribe still fail (e.g. the
    consumer exists but is push-configured). Same contract: stay legacy."""
    nw = _make_worker()
    nw._running = True
    nw._worker = MagicMock()
    nw._worker.get_info = MagicMock(return_value=MagicMock(id="w-1"))

    js = MagicMock()
    js.add_consumer = AsyncMock()
    js.pull_subscribe = AsyncMock(side_effect=Exception("not a pull consumer"))

    assert await nw._bind_per_worker_consumer(js) is False
    assert nw._per_worker_topic is False
    assert nw._per_worker_fetch_task is None


# ── _ensure_subtask_transport still binds both ───────────────────


async def test_transport_binds_shared_and_per_worker():
    """Shared consumer is the hard dependency; per-worker is additive."""
    nw = _make_worker()
    nw._running = True

    shared_sub = MagicMock()
    per_worker_sub = MagicMock()
    js = _make_js([shared_sub, per_worker_sub])
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)
    nw._register_with_gateway = AsyncMock()

    worker = MagicMock()
    worker.max_capacity = 3
    worker.get_info = MagicMock(return_value=MagicMock(id="w-1"))
    nw._worker = worker

    assert await nw._ensure_subtask_transport() is True
    assert nw._subtask_js_available is True
    assert nw._subtask_pull_sub is shared_sub
    assert nw._per_worker_topic is True
    assert nw._per_worker_pull_sub is per_worker_sub
    assert js.add_consumer.await_count == 2
    nw._register_with_gateway.assert_awaited_once()

    for task in (nw._subtask_fetch_task, nw._per_worker_fetch_task):
        assert task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_transport_still_ready_when_only_pull_subscribe_fails_for_overflow():
    """Legacy gateway (stream without the per-worker wildcard): the shared
    bind still succeeds, so the worker registers and takes overflow work."""
    nw = _make_worker()
    nw._running = True

    shared_sub = MagicMock()
    js = MagicMock()
    # 1st add_consumer = shared (ok), 2nd = per-worker (fails).
    js.add_consumer = AsyncMock(side_effect=[None, Exception("no wildcard")])
    js.pull_subscribe = AsyncMock(return_value=shared_sub)
    nw._nc = MagicMock()
    nw._nc.jetstream = MagicMock(return_value=js)
    nw._register_with_gateway = AsyncMock()

    worker = MagicMock()
    worker.max_capacity = 3
    worker.get_info = MagicMock(return_value=MagicMock(id="w-1"))
    nw._worker = worker

    assert await nw._ensure_subtask_transport() is True
    assert nw._subtask_js_available is True
    assert nw._subtask_pull_sub is shared_sub
    assert nw._per_worker_topic is False
    assert nw._per_worker_fetch_task is None
    nw._register_with_gateway.assert_awaited_once()

    nw._subtask_fetch_task.cancel()
    try:
        await nw._subtask_fetch_task
    except asyncio.CancelledError:
        pass


# ── Heartbeat carries the placement signals ──────────────────────


async def _run_one_heartbeat_tick(nw: _NatsWorker) -> None:
    task = asyncio.create_task(nw._heartbeat_loop())
    await asyncio.sleep(0.05)  # one tick completes; the loop then sleeps 30s
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _heartbeat_worker(mode: str = "worker") -> _NatsWorker:
    nw = _make_worker(mode)
    nw._running = True
    worker = MagicMock()
    worker.send_heartbeat = AsyncMock(return_value={})
    worker.worker_id = "w-hb"
    worker.get_info = MagicMock(
        return_value=MagicMock(id="w-hb", capabilities=[], current_load=2, max_capacity=4)
    )
    worker.recent_files = MagicMock(return_value=["src/a.rs", "src/b.rs"])
    nw._worker = worker
    nw._publisher = MagicMock()
    nw._publisher.publish_heartbeat = AsyncMock(return_value=True)
    nw._orchestrator = None
    nw._grpc_endpoint = ""
    engine = MagicMock()
    engine.worker_heartbeat_async = AsyncMock(return_value=True)
    nw._grpc_reg_engine = engine
    return nw


async def test_heartbeat_sends_recent_files_and_per_worker_topic():
    nw = _heartbeat_worker()
    nw._subtask_js_available = True
    nw._per_worker_topic = True

    await _run_one_heartbeat_tick(nw)

    args, _kwargs = nw._grpc_reg_engine.worker_heartbeat_async.await_args
    assert args[0] == "w-hb"
    assert args[1] == 2
    assert args[2] == CONTRACT_VERSION
    assert list(args[3]) == ["src/a.rs", "src/b.rs"]
    assert args[4] is True

    # Same declaration on the NATS w_info (operator visibility).
    w_info = nw._publisher.publish_heartbeat.await_args.args[1]
    assert w_info["per_worker_topic"] is True
    assert w_info["subtask_transport"] == "jetstream"


async def test_heartbeat_sends_neutral_signals_when_not_a_worker():
    """Mode "default" never binds a per-worker consumer, so it must declare
    False/empty rather than leaking a stale True."""
    nw = _heartbeat_worker(mode="default")
    nw._per_worker_topic = True  # even if some other path flipped it

    await _run_one_heartbeat_tick(nw)

    args, _kwargs = nw._grpc_reg_engine.worker_heartbeat_async.await_args
    assert list(args[3]) == []
    assert args[4] is False


async def test_heartbeat_reports_legacy_when_per_worker_bind_failed():
    nw = _heartbeat_worker()
    nw._subtask_js_available = True
    nw._per_worker_topic = False  # bind failed

    await _run_one_heartbeat_tick(nw)

    args, _kwargs = nw._grpc_reg_engine.worker_heartbeat_async.await_args
    assert args[4] is False

    w_info = nw._publisher.publish_heartbeat.await_args.args[1]
    assert w_info["per_worker_topic"] is False


# ── Engine passthrough ───────────────────────────────────────────


def test_engine_heartbeat_forwards_placement_signals():
    eng = Engine.__new__(Engine)
    eng._mode = "grpc"
    inner = MagicMock()
    inner.worker_heartbeat_async = AsyncMock(return_value=True)
    eng._grpc_engine = inner

    async def _run():
        return await eng.worker_heartbeat_async("w-1", 3, CONTRACT_VERSION, ["src/a.rs"], True)

    assert asyncio.run(_run()) is True
    args = inner.worker_heartbeat_async.await_args.args
    assert args == ("w-1", 3, CONTRACT_VERSION, ["src/a.rs"], True)


def test_engine_heartbeat_normalizes_empty_recent_files_to_none():
    """An empty list carries no signal; it is forwarded as None so the pyo3
    default kicks in rather than sending a misleading empty vector."""
    eng = Engine.__new__(Engine)
    eng._mode = "grpc"
    inner = MagicMock()
    inner.worker_heartbeat_async = AsyncMock(return_value=True)
    eng._grpc_engine = inner

    async def _run():
        return await eng.worker_heartbeat_async("w-1", 0, None, [], False)

    asyncio.run(_run())
    args = inner.worker_heartbeat_async.await_args.args
    assert args[3] is None
    assert args[4] is False


def test_engine_heartbeat_returns_false_when_not_in_grpc_mode():
    eng = Engine.__new__(Engine)
    eng._mode = "local"
    eng._grpc_engine = None

    async def _run():
        return await eng.worker_heartbeat_async("w-1", 0, None, ["src/a.rs"], True)

    assert asyncio.run(_run()) is False


# ── Worker.record_recent_files ───────────────────────────────────


def test_record_recent_files_is_bounded_deduped_newest_first():
    w = Worker(worker_id="w-aff", engine=None)

    w.record_recent_files(["src/a.rs", "src/b.rs"])
    assert w.recent_files() == ["src/a.rs", "src/b.rs"]

    # Re-touching moves to the front rather than duplicating.
    w.record_recent_files(["src/a.rs"])
    assert w.recent_files() == ["src/a.rs", "src/b.rs"]

    # Blanks dropped, whitespace trimmed.
    w.record_recent_files(["  src/c.rs  ", "", "   "])
    assert w.recent_files() == ["src/c.rs", "src/a.rs", "src/b.rs"]

    # Bounded: only the newest MAX_RECENT_FILES survive. Within one call the
    # earlier entries count as more recent, so they win truncation — callers
    # pass their primary signal first (declared files ahead of the diff).
    w.record_recent_files([f"src/f{i}.rs" for i in range(200)])
    snapshot = w.recent_files()
    assert len(snapshot) == Worker.MAX_RECENT_FILES
    assert snapshot[0] == "src/f0.rs"
    assert "src/f199.rs" not in snapshot


def test_record_recent_files_ignores_empty_input_and_never_raises():
    w = Worker(worker_id="w-aff", engine=None)
    w.record_recent_files([])
    w.record_recent_files(None)
    assert w.recent_files() == []

    # A non-iterable must not blow up an otherwise successful execution.
    w.record_recent_files(12345)
    assert w.recent_files() == []


def test_recent_files_returns_a_copy():
    w = Worker(worker_id="w-aff", engine=None)
    w.record_recent_files(["src/a.rs"])
    snapshot = w.recent_files()
    snapshot.append("mutated")
    assert w.recent_files() == ["src/a.rs"]


def test_recent_files_start_empty():
    assert Worker(worker_id="w-aff", engine=None).recent_files() == []


# ── _execute_in_sandbox feeds the summary ────────────────────────


def test_execute_in_sandbox_records_declared_and_changed_files():
    async def _run():
        w = Worker(worker_id="w-aff", engine=None)
        out = AgentOutput(
            summary="ok",
            success=True,
            file_changes=[FileChange(file_path="src/changed.py")],
        )
        w._sandbox_manager = MagicMock()
        w._sandbox_manager.execute = AsyncMock(return_value=out)
        subtask = Subtask(
            id="st-1",
            parent_id="t-1",
            description="d",
            file_constraints=["src/declared.py"],
        )
        res = await w._execute_in_sandbox(subtask)
        return w, res

    w, res = asyncio.run(_run())
    assert res.success is True
    # Declared ∪ changed — the declaration is the primary signal (a read-only
    # node has no file_changes at all), the diff is corroborating evidence.
    assert w.recent_files() == ["src/declared.py", "src/changed.py"]


def test_execute_in_sandbox_records_nothing_when_there_is_nothing_to_record():
    async def _run():
        w = Worker(worker_id="w-aff", engine=None)
        out = AgentOutput(summary="ok", success=True, file_changes=[])
        w._sandbox_manager = MagicMock()
        w._sandbox_manager.execute = AsyncMock(return_value=out)
        await w._execute_in_sandbox(
            Subtask(id="st-1", parent_id="t-1", description="d")
        )
        return w

    assert asyncio.run(_run()).recent_files() == []


# ── stop() tears the per-worker loop down ────────────────────────


def test_stop_cancels_per_worker_fetch_loop():
    async def _run():
        nw = _make_worker()
        nw._running = True
        nw._per_worker_topic = True
        nw._per_worker_pull_sub = MagicMock()

        async def _forever():
            await asyncio.Event().wait()

        nw._subtask_fetch_task = asyncio.create_task(_forever())
        nw._per_worker_fetch_task = asyncio.create_task(_forever())
        await asyncio.sleep(0)

        await nw.stop()
        return nw

    nw = asyncio.run(_run())
    # The per-worker loop is gone and the declaration is withdrawn, so a
    # stopped worker can never be targeted.
    assert nw._per_worker_fetch_task is None
    assert nw._per_worker_pull_sub is None
    assert nw._per_worker_topic is False
