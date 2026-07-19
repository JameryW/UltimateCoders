"""F58 — dashboard NATS must connect on the server's event loop.

The old code connected on a throwaway loop and closed it, killing nats-py's
reader/ping tasks; the carried-over client never delivered a message while
startup logged "Connected to NATS". The connect+subscribe now runs as the
FastAPI startup hook (uvicorn's loop).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.dashboard.app import (
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
    client.subscribe.assert_awaited_once()
    args, kwargs = client.subscribe.await_args
    subject = args[0] if args else kwargs.get("subject")
    assert subject == NATS_SUBJECT_TASK_EVENT
    assert len(app._nats_subscriptions) == 1
    assert app._owns_nats_client is False


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
