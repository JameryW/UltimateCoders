"""Regression tests for the dashboard SSE event stream.

Covers the ``/dashboard/api/stream`` generator. A prior bug yielded a snapshot
on every idle tick but only *computed* it inside the 10s-interval guard, so the
first idle iteration referenced an unbound ``snapshot`` variable and crashed the
generator with ``UnboundLocalError`` — killing the stream for every dashboard
client (notably in gateway-only / no-NATS deployments).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from ultimate_coders.dashboard.app import DashboardApp


def _make_app(nats_client: object | None = None) -> DashboardApp:
    app = DashboardApp(orchestrator=MagicMock(), nats_client=nats_client)
    app.event_emitter = None
    app._metrics = MagicMock()
    app._metrics.snapshot.return_value = {}
    app._metrics.record_event = MagicMock()
    app._check_auth = lambda _r: None
    return app


def _stream_route(app: DashboardApp) -> object:
    return next(
        r for r in app._app.router.routes
        if getattr(r, "path", "") == "/dashboard/api/stream"
    )


async def _drive(app: DashboardApp, max_iters: int) -> tuple[list, Exception | None]:
    """Run the SSE generator for up to ``max_iters`` idle iterations."""
    route = _stream_route(app)
    req = SimpleNamespace()
    calls = {"n": 0}

    async def is_disc() -> bool:
        calls["n"] += 1
        return calls["n"] > max_iters

    req.is_disconnected = is_disc
    resp = await route.endpoint(req)  # type: ignore[attr-defined]
    out: list = []
    err: Exception | None = None
    try:
        async for item in resp.body_iterator:
            out.append(item)
    except Exception as e:  # noqa: BLE001 — we want to capture any crash
        err = e
    return out, err


def test_stream_no_nats_does_not_crash_on_idle() -> None:
    """No NATS + a few quick idle ticks must not raise UnboundLocalError.

    Previously the generator computed ``snapshot`` only inside the 10s-interval
    guard but yielded it unconditionally → first idle tick crashed.
    """
    app = _make_app(nats_client=None)
    app._get_full_snapshot = lambda: {"ok": True}

    out, err = asyncio.run(_drive(app, max_iters=3))

    assert err is None, f"stream crashed: {err!r}"
    # Under 10s of idle ticks → no full snapshot should be emitted.
    assert out == []


def test_stream_emits_snapshot_after_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the 10s interval has elapsed, a full snapshot is emitted.

    We force the interval to elapse by patching ``loop.time`` so the first
    read (used to seed ``last_snapshot``) returns 0 and the next returns 11s.
    """
    app = _make_app(nats_client=None)
    computed: list[int] = []

    def snap() -> dict:
        computed.append(len(computed) + 1)
        return {"ok": True, "i": computed[-1]}

    app._get_full_snapshot = snap

    # Patch asyncio.get_running_loop().time via a fake loop. The generator calls
    # loop.time() twice per idle tick (once for heartbeat, once for snapshot).
    times = iter([0.0, 0.0, 0.0, 11.0, 11.0, 11.0, 11.0, 11.0])

    class _FakeLoop:
        def time(self) -> float:
            return next(times)

    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _FakeLoop()
    )

    out, err = asyncio.run(_drive(app, max_iters=3))
    assert err is None, f"stream crashed: {err!r}"
    assert len(computed) >= 1, "expected at least one snapshot after interval elapsed"
    updates = [o for o in out if isinstance(o, dict) and o.get("event") == "update"]
    assert len(updates) >= 1
    assert json.loads(updates[0]["data"])["ok"] is True


def test_stream_snapshot_payload_shape() -> None:
    """A yielded snapshot has the expected SSE envelope (id/event/data)."""
    app = _make_app(nats_client=None)
    app._get_full_snapshot = lambda: {"tasks": [], "workers": []}

    # Force the interval to elapse by monkeypatching loop.time via the generator
    # is hard; instead drive the NATS path with a pre-seeded queue so an event
    # is yielded and we can assert its shape.
    app._nats_client = MagicMock()  # truthy → enters NATS branch

    async def run() -> tuple[list, Exception | None]:
        # asyncio.Queue must be created inside the running loop (Py3.9 binds
        # a loop at construction). F63: the generator subscribes via
        # _subscribe_sse — patch it to return our pre-seeded queue.
        q: asyncio.Queue = asyncio.Queue()
        app._subscribe_sse = lambda: q
        app._unsubscribe_sse = lambda _q: None
        await q.put({"type": "subtask_completed", "data": {"id": "st-1"}})
        # max_iters=1: one iteration consumes the queued event and yields it;
        # a second iteration would block 2s on an empty queue (wait_for
        # timeout) for no extra shape coverage.
        return await _drive(app, max_iters=1)

    out, err = asyncio.run(run())
    assert err is None
    # At least one task_event should have been emitted
    events = [o for o in out if isinstance(o, dict) and o.get("event") == "task_event"]
    assert len(events) >= 1
    payload = json.loads(events[0]["data"])
    assert payload["type"] == "subtask_completed"


def test_nats_event_queue_bounded_drops_on_full(caplog: pytest.LogCaptureFixture) -> None:
    """Regression: event queues must stay bounded — a stalled client's burst
    can't grow memory without limit. F63: each SSE client queue is
    maxsize=1000; a full client queue drops + warns without affecting others."""
    app = _make_app(nats_client=None)

    async def run() -> None:
        # Bounded per-client queue, filled to capacity.
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        app._sse_subscribers.add(q)
        await q.put({"type": "a"})
        await q.put({"type": "b"})

        # Third event overflows → put_nowait raises QueueFull → caught + warned.
        msg = SimpleNamespace(data=json.dumps({"type": "c"}).encode())
        await app._handle_nats_event(msg)

        # Queue still holds only 2 (the overflow was dropped, not enqueued).
        assert q.qsize() == 2

    asyncio.run(run())
    assert any("queue full" in r.message.lower() for r in caplog.records)


def test_fanout_every_client_gets_every_event() -> None:
    """F63: the old shared queue load-balanced events between SSE clients —
    multi-tab dashboards each saw a random subset. Now every subscriber
    queue receives every event, and log/metrics record exactly once."""
    app = _make_app(nats_client=MagicMock())

    async def run() -> tuple[asyncio.Queue, asyncio.Queue]:
        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()
        app._sse_subscribers.add(q1)
        app._sse_subscribers.add(q2)

        for t in ("subtask_start", "subtask_end", "task_complete"):
            msg = SimpleNamespace(data=json.dumps({"type": t}).encode())
            await app._handle_nats_event(msg)

        assert q1.qsize() == 3 and q2.qsize() == 3
        types1 = [q1.get_nowait()["type"] for _ in range(3)]
        types2 = [q2.get_nowait()["type"] for _ in range(3)]
        assert types1 == types2 == ["subtask_start", "subtask_end", "task_complete"]
        return q1, q2

    asyncio.run(run())
    # Recorded once per event (not once per client): 3 events.
    assert app._metrics.record_event.call_count == 3
    assert len(app._event_log) == 3


def test_events_recorded_with_zero_sse_clients() -> None:
    """F63: recording used to live in the client loop — with no clients
    attached, REST /events and metrics lost everything. Now _handle_nats_event
    records unconditionally."""
    app = _make_app(nats_client=MagicMock())
    assert len(app._sse_subscribers) == 0

    async def run() -> None:
        msg = SimpleNamespace(data=json.dumps({"type": "task_complete"}).encode())
        await app._handle_nats_event(msg)

    asyncio.run(run())
    assert len(app._event_log) == 1
    assert app._metrics.record_event.call_count == 1

