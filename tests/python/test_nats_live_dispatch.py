"""Live-broker tests for the T12 (#654) dispatch plane.

Every other test of this feature is `MagicMock`-based: `js.add_consumer` is
an `AsyncMock`, so the broker's *opinion* of the consumer configuration is
never consulted. That blind spot is not hypothetical — it hid a defect that
made the whole feature inert:

    A work-queue stream admits exactly ONE unfiltered consumer ("multiple
    non-filtered consumers not allowed on workqueue stream", err_code
    10099) and refuses a filtered consumer that overlaps one already there
    ("filtered consumer not unique on workqueue stream", err_code 10100).
    The shared durable `subtask-workers` was created with no
    `filter_subject`, so it covered every subject in `UC_SUBTASKS` and the
    per-worker consumer could never be created. `_bind_per_worker_consumer`
    swallows that refusal by design (best-effort, worker stays legacy), so
    `_per_worker_topic` was False forever, every heartbeat declared false,
    and `WorkerRegistry::placement_target` — which filters on exactly that
    flag — never returned a target. Affinity placement silently degraded to
    pre-T12 behaviour, which the soft-semantics design makes indistinguishable
    from "no affinity was warranted".

These tests talk to a real `nats-server -js` and ask the server what it
thinks. They run the real `NatsWorker` binding paths (`_ensure_subtask_transport`
and `_bind_per_worker_consumer`) rather than re-implementing them, so a
regression in either is caught here.

Skipped unless `--integration` is passed (the `integration` marker is
auto-skipped by `tests/python/conftest.py`), and skipped again at runtime if
no broker is reachable — `UC_NATS_URL`, defaulting to `127.0.0.1:4222`
(spelled numerically, not `localhost`, to avoid an IPv6-first resolve).

They assert the stream's own config when it already exists, which is how a
drift between `uc-grpc-server::ensure_subtasks_stream` and this file's golden
surfaces. That also means running them against a broker a live gateway is
using will purge that gateways's in-flight dispatch state — intended for
local verification, not for a shared host.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from unittest.mock import AsyncMock

import nats
import pytest
from ultimate_coders.nats_worker import NatsWorker

pytestmark = pytest.mark.integration

NATS_URL = os.environ.get("UC_NATS_URL", "nats://127.0.0.1:4222")
STREAM = "UC_SUBTASKS"
SHARED_SUBJECT = "uc.subtask.execute"
PER_WORKER_WILDCARD = "uc.subtask.execute.w.>"
SHARED_DURABLE = "subtask-workers"


def per_worker_subject(worker_id: str) -> str:
    return NatsWorker._per_worker_subject(worker_id)


def per_worker_durable(worker_id: str) -> str:
    return NatsWorker._per_worker_durable(worker_id)


async def _broker_or_skip():
    """Connect, or skip when no broker answers.

    `nats.connect` performs the protocol handshake (INFO / PING / PONG), so a
    success here is proof of a live NATS server rather than of an open port.
    """
    try:
        return await nats.connect(NATS_URL, connect_timeout=2, max_reconnect_attempts=0)
    except Exception as exc:  # noqa: BLE001 - any failure means "no broker"
        pytest.skip(f"no NATS broker at {NATS_URL}: {exc}")


async def _ensure_stream(js) -> None:
    """Reuse the stream if present (and check its config), else create it.

    The creation config mirrors `uc-grpc-server::ensure_subtasks_stream`,
    which is the authority. When a gateway already provisioned the stream,
    asserting against the live config catches drift — including a pre-T12
    stream that was never given the per-worker wildcard.
    """
    expected_subjects = sorted([SHARED_SUBJECT, PER_WORKER_WILDCARD])
    try:
        info = await js.stream_info(STREAM)
    except Exception:  # noqa: BLE001 - absent stream is the normal first run
        await js.add_stream(
            name=STREAM,
            subjects=[SHARED_SUBJECT, PER_WORKER_WILDCARD],
            retention="workqueue",
            max_age=7 * 24 * 3600,
            duplicate_window=120,
        )
        info = await js.stream_info(STREAM)
    assert sorted(info.config.subjects or []) == expected_subjects, (
        "UC_SUBTASKS must carry BOTH the shared dispatch subject and the "
        "per-worker wildcard: `>` needs a further token, so it never covers "
        "the bare subject, and the gateway reaches targeted workers through "
        "the wildcard. A stream provisioned before T12 is repaired by "
        "`ensure_subtasks_stream`, which adds the missing wildcard in place — "
        "`get_or_create_stream` alone would leave it untouched, and without "
        "the wildcard every per-worker publish is a silent black hole."
    )
    assert str(info.config.retention).lower().endswith("workqueue"), (
        "exactly-once delivery between the shared queue and a per-worker "
        "durable depends on work-queue retention"
    )


async def _reset_stream_state(js) -> None:
    """Clear messages and consumers so per-test counts are unambiguous."""
    for durable in (SHARED_DURABLE, per_worker_durable("w-alpha"), per_worker_durable("w-beta")):
        try:
            await js.delete_consumer(STREAM, durable)
        except Exception:  # noqa: BLE001 - absent consumer is the normal case
            pass
    await js.purge_stream(STREAM)


async def _park_fetch_loops(nw) -> None:
    """Cancel both pull loops and wait for them to unwind.

    The durables stay bound — only the consumers' *readers* stop, which is
    what lets the server's pending count be read as the routing verdict.
    """
    for task in (nw._subtask_fetch_task, nw._per_worker_fetch_task):
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@asynccontextmanager
async def _bound_worker(nc, worker_id: str):
    """Run the real transport-bind path, then park the fetch loops.

    Binding is what is under test; consuming is not. A real dispatch would
    need an agent to execute it, so the fetch loops are stopped right after
    the bind, leaving both durables in place for the server to route into.
    Where a message lands is then read from the server, not from the worker.
    """
    nw = NatsWorker(project_path=".", mode="worker", nats_url=NATS_URL)
    nw._nc = nc
    nw._running = True
    nw._stopping = False
    nw._consumer_id = worker_id
    # gRPC registration is not part of the dispatch plane under test.
    nw._register_with_gateway = AsyncMock()

    try:
        ok = await nw._ensure_subtask_transport()
    finally:
        # Park the fetch loops BEFORE the caller publishes: an unparked loop
        # would consume and ack the message first, and the server's count
        # would then read 0 wherever the message had actually been routed.
        nw._running = False
        nw._stopping = True
        await _park_fetch_loops(nw)

    try:
        yield nw, ok
    finally:
        nw._running = False
        nw._stopping = True
        await _park_fetch_loops(nw)


async def _outstanding(js, durable: str) -> int:
    """Messages this consumer owns: waiting + delivered-but-unacked.

    Both halves are needed. The worker's fetch loop may already have a pull
    request in flight when it is parked (`_ensure_subtask_transport` awaits
    between starting the two loops, so the shared one usually gets its first
    `fetch()` request out and the per-worker one sometimes does not); a
    message handed to such a request is *delivered* and sits in
    `num_ack_pending`, never in `num_pending`. Counting only `num_pending`
    would then read 0 for a message this consumer demonstrably received.
    Summing the two is the routing verdict in either case, and stays 0 for
    the consumer the server did NOT pick.
    """
    info = await js.consumer_info(STREAM, durable)
    return (info.num_pending or 0) + (info.num_ack_pending or 0)


async def _wait_for_outstanding(js, durable: str, expected: int, timeout: float = 3.0) -> int:
    """Poll a consumer's owned-message count — routing is asynchronous."""
    deadline = asyncio.get_event_loop().time() + timeout
    seen = -1
    while asyncio.get_event_loop().time() < deadline:
        seen = await _outstanding(js, durable)
        if seen == expected:
            return seen
        await asyncio.sleep(0.05)
    return seen


async def test_shared_and_per_worker_consumers_coexist():
    """The shipped configuration could not: the shared durable was unfiltered.

    An unfiltered consumer is legal only while it is the only one, so binding
    the per-worker durable was refused with err_code 10100 — and that refusal
    is swallowed by `_bind_per_worker_consumer`'s best-effort handler, leaving
    `_per_worker_topic` False and the worker permanently untargetable.
    """
    nc = await _broker_or_skip()
    js = nc.jetstream()
    try:
        await _ensure_stream(js)
        await _reset_stream_state(js)

        async with _bound_worker(nc, "w-alpha") as (nw, ok):
            assert ok is True
            assert nw._subtask_js_available is True
            # The gateway targets a worker ONLY on this declaration.
            assert nw._per_worker_topic is True, (
                "the per-worker durable failed to bind, so this worker would "
                "declare per_worker_topic=false forever and never be targeted"
            )

            pw = await js.consumer_info(STREAM, per_worker_durable("w-alpha"))
            assert pw.config.filter_subject == per_worker_subject("w-alpha")

            shared = await js.consumer_info(STREAM, SHARED_DURABLE)
            assert shared.config.filter_subject == SHARED_SUBJECT, (
                "the shared durable must be pinned to the bare shared "
                "subject: unfiltered it covers the per-worker subjects too, "
                "which both makes a per-worker consumer illegal and lets the "
                "overflow queue steal targeted messages"
            )
    finally:
        await nc.drain()


async def test_two_workers_bind_distinct_per_worker_durables():
    """Filters must stay unique across workers, not just against the shared one."""
    nc = await _broker_or_skip()
    js = nc.jetstream()
    try:
        await _ensure_stream(js)
        await _reset_stream_state(js)

        # Nested: both transports stay bound while the second binds.
        async with _bound_worker(nc, "w-alpha") as (nw_a, ok_a):
            assert ok_a is True and nw_a._per_worker_topic is True
            async with _bound_worker(nc, "w-beta") as (nw_b, ok_b):
                assert ok_b is True and nw_b._per_worker_topic is True
                for worker_id in ("w-alpha", "w-beta"):
                    info = await js.consumer_info(STREAM, per_worker_durable(worker_id))
                    assert info.config.filter_subject == per_worker_subject(worker_id)
    finally:
        await nc.drain()


async def test_targeted_publish_lands_on_the_targeted_worker_only():
    """A core publish to the per-worker subject — the gateway's own publish API."""
    nc = await _broker_or_skip()
    js = nc.jetstream()
    try:
        await _ensure_stream(js)
        await _reset_stream_state(js)

        async with _bound_worker(nc, "w-alpha") as (nw, ok):
            assert ok is True and nw._per_worker_topic is True

            # `Client::publish_with_headers` is a CORE publish: it returns Ok
            # even when no stream matches the subject, so a missing wildcard
            # is a silent black hole rather than an error.
            await nc.publish(
                per_worker_subject("w-alpha"), json.dumps({"probe": "targeted"}).encode()
            )
            await nc.flush()

            assert await _wait_for_outstanding(js, per_worker_durable("w-alpha"), 1) == 1, (
                "a targeted publish must reach the targeted worker's durable"
            )
            assert await _outstanding(js, SHARED_DURABLE) == 0, (
                "the shared overflow queue must not steal a targeted message"
            )
    finally:
        await nc.drain()


async def test_shared_publish_still_reaches_the_overflow_queue():
    """Placement is a soft preference — overflow must survive the filter."""
    nc = await _broker_or_skip()
    js = nc.jetstream()
    try:
        await _ensure_stream(js)
        await _reset_stream_state(js)

        async with _bound_worker(nc, "w-alpha") as (nw, ok):
            assert ok is True and nw._per_worker_topic is True

            await nc.publish(SHARED_SUBJECT, json.dumps({"probe": "shared"}).encode())
            await nc.flush()

            assert await _wait_for_outstanding(js, SHARED_DURABLE, 1) == 1, (
                "the shared subject is the overflow path: every node stays "
                "dispatchable without a placement score"
            )
            assert await _outstanding(js, per_worker_durable("w-alpha")) == 0
    finally:
        await nc.drain()


async def test_legacy_worker_is_served_by_overflow_alone():
    """A worker that never declared a per-worker topic still gets work.

    This is the deployment shape of every pre-T12 worker, and the shape every
    worker devolved to while the per-worker bind was failing: shared-only.
    """
    nc = await _broker_or_skip()
    js = nc.jetstream()
    try:
        await _ensure_stream(js)
        await _reset_stream_state(js)

        # Bind ONLY the shared durable, exactly as a legacy worker does.
        await js.add_consumer(
            stream=STREAM,
            durable_name=SHARED_DURABLE,
            filter_subject=SHARED_SUBJECT,
            ack_policy="explicit",
            max_deliver=5,
        )
        await nc.publish(SHARED_SUBJECT, json.dumps({"probe": "legacy"}).encode())
        await nc.flush()

        assert await _wait_for_outstanding(js, SHARED_DURABLE, 1) == 1, (
            "a shared-only worker must still receive overflow work"
        )
    finally:
        await nc.drain()
