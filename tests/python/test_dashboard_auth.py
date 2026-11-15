"""Dashboard authentication and fail-closed task mutations.

The dashboard can be reachable from the public internet (``uc.jameryw.dev``
fronts ``uc-api`` with nginx), and its submit route starts real work. "No
password configured" therefore must not mean "anyone may submit": task
mutations fail closed unless a secret is configured or the operator sets the
explicit development escape hatch. Reads stay available for monitoring.

These tests pin the three things that went wrong in the old gate:

* the gate disappeared entirely when ``DASHBOARD_PASSWORD`` was unset;
* every localhost caller bypassed it, including through an nginx proxy that
  connects from 127.0.0.1;
* token comparisons were ordinary string equality.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from ultimate_coders.dashboard.app import DashboardApp

REMOTE = ("203.0.113.1", 12345)
LOCAL = ("127.0.0.1", 12345)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch, tmp_path):
    """Start every test from the deployment's "nothing configured" state."""
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    for name in (
        "UC_DATABASE_URL",
        "DASHBOARD_PASSWORD",
        "UC_DASHBOARD_TOKEN",
        "UC_DASHBOARD_TRUST_LOCALHOST",
        "UC_DASHBOARD_ALLOW_OPEN_MUTATIONS",
    ):
        monkeypatch.delenv(name, raising=False)


def _client(app: DashboardApp, *, client=REMOTE, headers: dict | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app._app, client=client)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://dashboard.test",
        headers=headers or {},
    )


_UNSET = object()


def _app(*, orchestrator=_UNSET, publisher=None) -> DashboardApp:
    """Build the app; an explicit ``orchestrator=None`` stays None."""
    return DashboardApp(
        orchestrator=MagicMock() if orchestrator is _UNSET else orchestrator,
        nats_publisher=publisher,
    )


MUTATIONS = [
    ("post", "/dashboard/api/tasks/submit", {"description": "build it"}),
    ("post", "/dashboard/api/tasks/t1/pause", None),
    ("post", "/dashboard/api/tasks/t1/resume", None),
    ("post", "/dashboard/api/tasks/flush-pending", None),
    ("post", "/dashboard/api/scheduler/jobs/j1/trigger", None),
    ("post", "/dashboard/api/inference/operations/op1/reconcile", {"expected_version": 0}),
]


@pytest.mark.asyncio
async def test_task_mutations_fail_closed_without_a_configured_secret():
    app = _app(orchestrator=None, publisher=AsyncMock())
    async with _client(app) as client:
        for method, path, body in MUTATIONS:
            response = await getattr(client, method)(path, json=body)
            assert response.status_code == 503, (path, response.text)
            payload = response.json()
            assert payload["error"] == "Dashboard auth is not configured"
            assert "DASHBOARD_PASSWORD" in payload["detail"]


@pytest.mark.asyncio
async def test_reads_stay_open_without_a_secret():
    app = _app(orchestrator=None)
    async with _client(app) as client:
        response = await client.get("/dashboard/api/experiments")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_explicit_development_escape_hatch_allows_mutations(monkeypatch):
    monkeypatch.setenv("UC_DASHBOARD_ALLOW_OPEN_MUTATIONS", "1")
    app = _app()
    async with _client(app) as client:
        # Reaches body validation instead of the auth 503: the hatch opened it.
        response = await client.post("/dashboard/api/tasks/submit", json={"description": ""})
    assert response.status_code == 400
    assert response.json()["error"] == "description is required"


@pytest.mark.asyncio
async def test_a_configured_token_guards_every_mutation(monkeypatch):
    monkeypatch.setenv("UC_DASHBOARD_TOKEN", "s3cret")
    app = _app(orchestrator=None, publisher=AsyncMock())
    async with _client(app) as client:
        for method, path, body in MUTATIONS:
            response = await getattr(client, method)(path, json=body)
            assert response.status_code == 401, (path, response.text)
            assert response.json()["error"] == "Unauthorized"


@pytest.mark.asyncio
async def test_a_bearer_token_opens_submit_and_query_token_is_accepted(monkeypatch):
    monkeypatch.setenv("UC_DASHBOARD_TOKEN", "s3cret")
    publisher = AsyncMock()
    app = _app(orchestrator=None, publisher=publisher)

    async with _client(app, headers={"Authorization": "Bearer s3cret"}) as client:
        response = await client.post(
            "/dashboard/api/tasks/submit", json={"description": "build it"}
        )
    assert response.status_code == 200
    assert response.json()["success"] is True
    publisher.publish_submit.assert_awaited()

    async with _client(app) as client:
        response = await client.post(
            "/dashboard/api/tasks/submit?token=s3cret", json={"description": "build it"}
        )
    assert response.status_code == 200

    async with _client(app, headers={"Authorization": "Bearer wrong"}) as client:
        assert (
            await client.post("/dashboard/api/tasks/submit", json={"description": "build it"})
        ).status_code == 401


@pytest.mark.asyncio
async def test_localhost_does_not_bypass_auth_by_default(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    app = _app()
    async with _client(app, client=LOCAL) as client:
        response = await client.post(
            "/dashboard/api/tasks/submit", json={"description": "build it"}
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_trusted_localhost_is_opt_in_and_a_proxy_header_voids_it(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    monkeypatch.setenv("UC_DASHBOARD_TRUST_LOCALHOST", "1")
    app = _app()

    async with _client(app, client=LOCAL) as client:
        # Auth passed (local bypass) and validation rejected the empty body.
        response = await client.post("/dashboard/api/tasks/submit", json={"description": ""})
    assert response.status_code == 400

    async with _client(
        app, client=LOCAL, headers={"X-Forwarded-For": "8.8.8.8"}
    ) as client:
        response = await client.post("/dashboard/api/tasks/submit", json={"description": ""})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_password_wins_over_the_generic_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "password-secret")
    monkeypatch.setenv("UC_DASHBOARD_TOKEN", "gateway-secret")
    app = _app()

    async with _client(app, headers={"Authorization": "Bearer password-secret"}) as client:
        assert (await client.post("/dashboard/api/tasks/t1/pause")).status_code == 200
    async with _client(app, headers={"Authorization": "Bearer gateway-secret"}) as client:
        assert (await client.post("/dashboard/api/tasks/t1/pause")).status_code == 401
