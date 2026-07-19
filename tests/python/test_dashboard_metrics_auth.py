"""F62 — /metrics must respect DASHBOARD_PASSWORD auth.

Every other dashboard route gates on _check_auth; /metrics didn't, leaking
task/worker/error metrics (incl. worker ids) to unauthenticated clients
when the dashboard is network-exposed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.requests import Request
from starlette.responses import JSONResponse
from ultimate_coders.dashboard.app import DashboardApp


def _make_request(host: str = "10.0.0.1", query: bytes = b"") -> Request:
    """Non-localhost request (localhost bypasses auth by design)."""
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/metrics",
        "query_string": query,
        "headers": [],
        "client": (host, 12345),
    })


def _metrics_endpoint(app: DashboardApp):
    for route in app._app.routes:
        if getattr(route, "path", None) == "/metrics":
            return route.endpoint
    raise AssertionError("/metrics route not registered")


async def test_metrics_requires_auth_when_password_set(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pwd")
    app = DashboardApp(orchestrator=MagicMock())
    endpoint = _metrics_endpoint(app)

    resp = await endpoint(_make_request())
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 401

    # Correct token passes (query-param form).
    ok = await endpoint(_make_request(query=b"token=secret-pwd"))
    assert not isinstance(ok, JSONResponse) or ok.status_code == 200


async def test_metrics_open_when_no_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = DashboardApp(orchestrator=MagicMock())
    endpoint = _metrics_endpoint(app)
    resp = await endpoint(_make_request())
    assert not isinstance(resp, JSONResponse) or resp.status_code == 200
