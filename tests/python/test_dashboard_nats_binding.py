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
    await app._handle_dashboard_snapshot(
        SimpleNamespace(data=json.dumps(payload).encode("utf-8"))
    )

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
