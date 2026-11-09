"""F58 — dashboard NATS must connect on the server's event loop.

The old code connected on a throwaway loop and closed it, killing nats-py's
reader/ping tasks; the carried-over client never delivered a message while
startup logged "Connected to NATS". The connect+subscribe now runs as the
FastAPI startup hook (uvicorn's loop).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.dashboard.app import (
    NATS_SUBJECT_DASHBOARD_SNAPSHOT,
    NATS_SUBJECT_TASK_EVENT,
    DashboardApp,
)


async def test_connect_and_subscribe_with_dead_server_is_nonfatal():
    """Unreachable NATS degrades to snapshot-only — no exception, no client."""
    app = DashboardApp(orchestrator=None, nats_url="nats://127.0.0.1:1")
    await app._connect_and_subscribe_nats()
    assert app._nats_client is None
    assert app._nats_subscriptions == []


async def test_connect_and_subscribe_uses_injected_client():
    """A pre-connected client (tests/embedded) is subscribed directly — no
    reconnect attempt — and is NOT owned (caller drains it)."""
    client = MagicMock()
    client.subscribe = AsyncMock(return_value=MagicMock())
    app = DashboardApp(orchestrator=MagicMock(), nats_client=client)
    await app._connect_and_subscribe_nats()
    assert client.subscribe.await_count == 2
    subjects = {
        (call.args[0] if call.args else call.kwargs.get("subject"))
        for call in client.subscribe.await_args_list
    }
    assert subjects == {NATS_SUBJECT_TASK_EVENT, NATS_SUBJECT_DASHBOARD_SNAPSHOT}
    assert len(app._nats_subscriptions) == 2
    assert app._owns_nats_client is False


async def test_dashboard_snapshot_populates_standalone_fallback():
    """A dashboard without an Orchestrator serves the NATS snapshot."""
    import json
    from types import SimpleNamespace

    app = DashboardApp(orchestrator=None)
    payload = {
        "health": {"available": True, "status": "healthy"},
        "workers": {"available": True, "workers": [], "total": 0},
        "tasks": {"available": True, "tasks": [], "status_counts": {}},
        "scheduler": {"available": True, "running": True},
    }
    await app._handle_dashboard_snapshot(SimpleNamespace(data=json.dumps(payload).encode("utf-8")))

    assert app._get_health_data()["available"] is True
    assert app._get_workers_data()["available"] is True
    assert app._get_tasks_data()["available"] is True
    assert app._get_scheduler_data()["available"] is True


async def test_invalid_dashboard_snapshot_is_ignored():
    """Malformed snapshot messages must not take down the dashboard."""
    from types import SimpleNamespace

    app = DashboardApp(orchestrator=None)
    await app._handle_dashboard_snapshot(SimpleNamespace(data=b"not-json"))
    assert app._dashboard_snapshot is None


async def test_close_owned_nats_drains_only_self_connected():
    """Shutdown drain touches only clients this app connected itself."""
    client = MagicMock()
    client.drain = AsyncMock()
    app = DashboardApp(orchestrator=None)
    app._nats_client = client
    app._owns_nats_client = True
    await app._close_owned_nats()
    client.drain.assert_awaited_once()
    assert app._nats_client is None
    assert app._owns_nats_client is False

    # Not owned → untouched (an injected client is the caller's to drain).
    app._nats_client = client
    app._owns_nats_client = False
    await app._close_owned_nats()
    assert app._nats_client is client


class _FakeClient:
    """Minimal nats client: only what the retry path touches."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_a_connect_that_cannot_subscribe_is_not_claimed(monkeypatch):
    """Connected-but-unsubscribed must not look like success.

    Otherwise nothing retries (the client is set), the dashboard silently stops
    receiving events, and the only trace is a GC warning about the dead task.
    """
    client = _FakeClient()
    app = DashboardApp(orchestrator=None)

    async def connect():
        return client

    async def subscribe():
        raise RuntimeError("subjects unavailable")

    monkeypatch.setattr(app, "_connect_nats_once", connect)
    monkeypatch.setattr(app, "_subscribe_nats_subjects", subscribe)

    assert await app._retry_connect_once() is False
    assert app._nats_client is None
    assert app._owns_nats_client is False
    assert client.closed, "the half-attached client is closed, not leaked"
    assert app._nats_publisher is None


async def test_a_connect_that_subscribes_is_claimed(monkeypatch):
    client = _FakeClient()
    app = DashboardApp(orchestrator=None)

    async def connect():
        return client

    async def subscribe():
        return None

    monkeypatch.setattr(app, "_connect_nats_once", connect)
    monkeypatch.setattr(app, "_subscribe_nats_subjects", subscribe)

    assert await app._retry_connect_once() is True
    assert app._nats_client is client
    assert app._owns_nats_client is True
    assert app._nats_publisher is not None


async def test_the_retry_loop_keeps_trying_after_a_failed_subscribe(monkeypatch):
    """The loop survives one bad subscribe instead of dying with the task."""
    import asyncio

    client = _FakeClient()
    app = DashboardApp(orchestrator=None)
    attempts = {"connect": 0, "subscribe": 0}

    async def connect():
        attempts["connect"] += 1
        return client

    async def subscribe():
        attempts["subscribe"] += 1
        if attempts["subscribe"] == 1:
            raise RuntimeError("subjects unavailable")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(app, "_connect_nats_once", connect)
    monkeypatch.setattr(app, "_subscribe_nats_subjects", subscribe)

    app._spawn_nats_connect_retry()
    task = app._nats_retry_task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert attempts["subscribe"] == 2
    assert attempts["connect"] == 2
    assert app._nats_client is client


async def test_dropping_an_owned_client_drops_the_publisher_bound_to_it():
    """A publisher left over the drained client would publish into a closed socket.

    The retry path only builds a publisher when there is none, so a stale one would
    never be replaced -- the retry would "succeed" while every publish failed.
    """
    from ultimate_coders.nats_worker import NatsPublisher

    client = _FakeClient()
    app = DashboardApp(orchestrator=None)
    app._nats_client = client
    app._owns_nats_client = True
    app._nats_publisher = NatsPublisher(client)

    await app._close_owned_nats()

    assert app._nats_client is None
    assert app._nats_publisher is None


async def test_a_publisher_bound_to_another_client_is_left_alone():
    """An injected client's publisher is the caller's; shutdown must not clear it."""
    from ultimate_coders.nats_worker import NatsPublisher

    injected = _FakeClient()
    owned = _FakeClient()
    app = DashboardApp(orchestrator=None)
    app._nats_publisher = NatsPublisher(injected)
    app._nats_client = owned
    app._owns_nats_client = True

    await app._close_owned_nats()

    assert app._nats_client is None
    assert app._nats_publisher is not None


async def test_the_retry_rebinds_a_fresh_publisher(monkeypatch):
    """After a close, the next connect gets a publisher over the *new* client."""
    from ultimate_coders.nats_worker import NatsPublisher

    old = _FakeClient()
    new = _FakeClient()
    app = DashboardApp(orchestrator=None)
    app._nats_client = old
    app._owns_nats_client = True
    app._nats_publisher = NatsPublisher(old)
    await app._close_owned_nats()
    assert app._nats_publisher is None

    async def connect():
        return new

    async def subscribe():
        return None

    monkeypatch.setattr(app, "_connect_nats_once", connect)
    monkeypatch.setattr(app, "_subscribe_nats_subjects", subscribe)

    assert await app._retry_connect_once() is True
    assert app._nats_client is new
    assert app._nats_publisher is not None
    assert app._nats_publisher._nc is new
