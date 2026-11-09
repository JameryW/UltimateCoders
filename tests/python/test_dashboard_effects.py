"""Dashboard read surface for EffectLedger receipts (D23 / P13).

Read-only: the queue of effects nobody has decided on (``UNKNOWN`` by default), plus
``stale=1`` for ``RECONCILING`` receipts whose soft lease expired. Nothing here writes;
reconciliation stays with the operator CLI and the merge arbiter.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from ultimate_coders.dashboard.app import DashboardApp
from ultimate_coders.effect_ledger import EffectLedger, EffectStatus, stable_effect_id
from ultimate_coders.runtime_state import RuntimeState


def _request(query: str = "", host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/dashboard/api/effects",
            "query_string": query.encode(),
            "headers": [],
            "client": (host, 12345),
        }
    )


def _endpoint(app: DashboardApp):
    for route in app._app.routes:
        if getattr(route, "path", None) == "/dashboard/api/effects":
            return route.endpoint
    raise AssertionError("effects route not registered")


@pytest.fixture
def app_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = DashboardApp(orchestrator=MagicMock())
    return app, RuntimeState()


def _seed(
    state: RuntimeState,
    name: str,
    status: EffectStatus,
    *,
    lease_seconds: float | None = None,
):
    ledger = EffectLedger(state)
    effect_id = stable_effect_id("push-branch", name)
    ledger.prepare(effect_id, kind="push-branch", request={"branch": f"uc/subtask/{name}"})
    if status is EffectStatus.PREPARED:
        return effect_id
    if status is EffectStatus.UNKNOWN:
        ledger.mark_in_flight(effect_id, owner="w1")
        ledger.mark_unknown(effect_id, reason="lost response")
        return effect_id
    if status is EffectStatus.RECONCILING:
        ledger.mark_in_flight(effect_id, owner="w1")
        ledger.mark_unknown(effect_id, reason="lost response")
        ledger.begin_reconcile(effect_id, owner="ops:a", lease_seconds=lease_seconds or 120)
        return effect_id
    ledger.mark_in_flight(effect_id, owner="w1")
    if status is EffectStatus.CONFIRMED:
        ledger.confirm(effect_id)
    else:
        ledger.fail(effect_id, error="rejected")
    return effect_id


async def _payload(resp) -> dict:
    return json.loads(resp.body)


async def test_respects_dashboard_password_auth(app_and_state, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pwd")
    app = DashboardApp(orchestrator=MagicMock())
    resp = await _endpoint(app)(_request(host="10.0.0.1"))
    assert resp.status_code == 401


async def test_the_default_view_is_the_unknown_queue(app_and_state):
    app, state = app_and_state
    unknown = _seed(state, "a", EffectStatus.UNKNOWN)
    _seed(state, "b", EffectStatus.CONFIRMED)
    _seed(state, "c", EffectStatus.FAILED)

    resp = await _endpoint(app)(_request())
    payload = await _payload(resp)
    assert resp.status_code == 200
    assert [row["effect_id"] for row in payload["effects"]] == [unknown]
    assert payload["total"] == 1
    assert payload["next_after"] is None
    row = payload["effects"][0]
    assert row["kind"] == "push-branch"
    assert row["status"] == "UNKNOWN"
    assert row["reason"] == "lost response"
    assert row["lease_expired"] is False


async def test_unknown_status_is_refused(app_and_state):
    app, _state = app_and_state
    resp = await _endpoint(app)(_request("status=HOLDING"))
    assert resp.status_code == 400


async def test_limit_bounds_are_enforced(app_and_state):
    app, _state = app_and_state
    for query in ("limit=0", "limit=1001", "limit=abc"):
        resp = await _endpoint(app)(_request(query))
        assert resp.status_code == 400, query


async def test_stale_requires_the_reconciling_status(app_and_state):
    app, _state = app_and_state
    resp = await _endpoint(app)(_request("stale=1"))
    assert resp.status_code == 400


async def test_stale_lists_only_expired_reconcile_leases(app_and_state):
    app, state = app_and_state
    live = _seed(state, "live", EffectStatus.RECONCILING, lease_seconds=120)
    expired = _seed(state, "expired", EffectStatus.RECONCILING, lease_seconds=-1)

    resp = await _endpoint(app)(_request("status=RECONCILING&stale=1"))
    payload = await _payload(resp)
    assert resp.status_code == 200
    assert [row["effect_id"] for row in payload["effects"]] == [expired]
    assert payload["effects"][0]["lease_expired"] is True
    assert payload["effects"][0]["owner"] == "ops:a"

    everyone = await _payload(await _endpoint(app)(_request("status=RECONCILING")))
    assert {row["effect_id"] for row in everyone["effects"]} == {live, expired}


async def test_after_pages_through_the_queue(app_and_state):
    app, state = app_and_state
    ids = sorted(
        _seed(state, name, EffectStatus.UNKNOWN) for name in ("a", "b", "c")
    )

    page = await _payload(await _endpoint(app)(_request("limit=2")))
    assert [row["effect_id"] for row in page["effects"]] == ids[:2]
    assert page["next_after"] == ids[1]
    assert page["total"] == 3

    rest = await _payload(await _endpoint(app)(_request(f"limit=2&after={ids[1]}")))
    assert [row["effect_id"] for row in rest["effects"]] == ids[2:]
    assert rest["next_after"] is None


async def test_a_receipt_reports_its_request_without_secrets(app_and_state):
    app, _state = app_and_state
    _seed(_state, "a", EffectStatus.UNKNOWN)
    payload = await _payload(await _endpoint(app)(_request()))
    assert payload["effects"][0]["request"] == {"branch": "uc/subtask/a"}


async def test_an_unknown_cursor_is_refused_not_paged_past(app_and_state):
    app, state = app_and_state
    _seed(state, "a", EffectStatus.UNKNOWN)
    resp = await _endpoint(app)(_request("after=eff_does_not_exist"))
    assert resp.status_code == 400
    assert (await _payload(resp))["error"] == "Unknown cursor in this scope"


# ---------------------------------------------------------------------------
# Over real HTTP (T52): the route is part of the authenticated surface, and the
# acceptance criterion "unauthenticated call is refused like the other API
# routes" had no test proving it. TestClient reports a non-localhost host, so a
# configured DASHBOARD_PASSWORD is actually enforced here.
# ---------------------------------------------------------------------------


def _http_client(monkeypatch, tmp_path, *, password: str | None = None):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime-http"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    if password is None:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    app = DashboardApp(orchestrator=MagicMock())
    return TestClient(app._app), RuntimeState()


def test_an_unauthenticated_remote_caller_is_refused(monkeypatch, tmp_path):
    client, state = _http_client(monkeypatch, tmp_path, password="s3cret")
    _seed(state, "a", EffectStatus.UNKNOWN)

    response = client.get("/dashboard/api/effects")
    assert response.status_code == 401
    assert response.json()["error"] == "Unauthorized"
    assert "effects" not in response.json()

    # The wrong token is refused too -- a 401 must not be a formality.
    assert client.get("/dashboard/api/effects", params={"token": "nope"}).status_code == 401


def test_a_bearer_token_opens_the_receipt_queue(monkeypatch, tmp_path):
    client, state = _http_client(monkeypatch, tmp_path, password="s3cret")
    effect_id = _seed(state, "a", EffectStatus.UNKNOWN)

    response = client.get(
        "/dashboard/api/effects",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert [row["effect_id"] for row in payload["effects"]] == [effect_id]
    assert payload["effects"][0]["status"] == "UNKNOWN"


def test_a_query_token_opens_the_receipt_queue(monkeypatch, tmp_path):
    client, state = _http_client(monkeypatch, tmp_path, password="s3cret")
    _seed(state, "a", EffectStatus.UNKNOWN)

    assert client.get("/dashboard/api/effects", params={"token": "s3cret"}).status_code == 200


def test_bad_pagination_is_a_400_over_http(monkeypatch, tmp_path):
    client, _state = _http_client(monkeypatch, tmp_path)

    assert client.get("/dashboard/api/effects", params={"limit": 0}).status_code == 400
    assert client.get("/dashboard/api/effects", params={"status": "NOPE"}).status_code == 400
    assert client.get("/dashboard/api/effects", params={"stale": "1"}).status_code == 400


def test_the_http_response_carries_the_paging_cursor(monkeypatch, tmp_path):
    client, state = _http_client(monkeypatch, tmp_path)
    ids = sorted(_seed(state, name, EffectStatus.UNKNOWN) for name in ("a", "b", "c"))

    payload = client.get("/dashboard/api/effects", params={"limit": 2}).json()
    assert [row["effect_id"] for row in payload["effects"]] == ids[:2]
    assert payload["next_after"] == ids[1]

    following = client.get(
        "/dashboard/api/effects",
        params={"limit": 2, "after": payload["next_after"]},
    ).json()
    assert [row["effect_id"] for row in following["effects"]] == ids[2:]
