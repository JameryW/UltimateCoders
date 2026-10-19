"""Dashboard read surface for context manifests (P1b increment 2 follow-on).

The manifest outbox was write-only: the audit trail existed in the runtime
state but nothing could read it back over HTTP. These tests pin the
endpoint's contract: required identifiers, bounded pagination, the
subtask-scoped history in key order, and the guarantee that the payload
carries the compiler's summary -- never prompt content.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from ultimate_coders.context_outbox import ContextManifestOutbox, manifest_key
from ultimate_coders.dashboard.app import DashboardApp
from ultimate_coders.runtime_state import RuntimeState


def _request(query: str = "", host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/dashboard/api/context/manifests",
            "query_string": query.encode(),
            "headers": [],
            "client": (host, 12345),
        }
    )


def _endpoint(app: DashboardApp):
    for route in app._app.routes:
        if getattr(route, "path", None) == "/dashboard/api/context/manifests":
            return route.endpoint
    raise AssertionError("context manifests route not registered")


@pytest.fixture
def app_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = DashboardApp(orchestrator=MagicMock())
    return app, RuntimeState()


def _summary(budget: int = 100) -> dict:
    return {
        "budget_tokens": budget,
        "total_tokens": 10,
        "stable_tokens": 10,
        "risk": "normal",
        "over_budget": False,
        "items_by_tier": {"P0 policy": 1},
        "dropped": [],
    }


async def test_requires_task_and_subtask_ids(app_and_state):
    app, _state = app_and_state
    endpoint = _endpoint(app)

    for query in ("", "task_id=t1", "subtask_id=s1"):
        resp = await endpoint(_request(query))
        assert resp.status_code == 400, query
        assert "task_id and subtask_id" in resp.body.decode()


async def test_rejects_unbounded_or_non_numeric_limit(app_and_state):
    app, _state = app_and_state
    endpoint = _endpoint(app)

    for limit in ("0", "1001", "abc"):
        resp = await endpoint(_request(f"task_id=t1&subtask_id=s1&limit={limit}"))
        assert resp.status_code == 400, limit


async def test_returns_the_subtask_history_in_key_order(app_and_state):
    app, state = app_and_state
    outbox = ContextManifestOutbox(state)
    outbox.record(manifest_key("t1", "s1", 0), summary=_summary(100))
    outbox.record(manifest_key("t1", "s1", 1), summary=_summary(200))
    outbox.record(manifest_key("t1", "s2", 0), summary=_summary(300))

    resp = await _endpoint(app)(_request("task_id=t1&subtask_id=s1"))
    payload = json.loads(resp.body)

    assert resp.status_code == 200
    assert payload["count"] == 2
    assert [m["key"] for m in payload["manifests"]] == ["t1:s1:0", "t1:s1:1"]
    assert payload["manifests"][0]["summary"]["budget_tokens"] == 100
    assert payload["manifests"][1]["summary"]["budget_tokens"] == 200


async def test_unknown_subtask_yields_an_empty_history_not_an_error(app_and_state):
    app, _state = app_and_state

    resp = await _endpoint(app)(_request("task_id=t1&subtask_id=nope"))
    payload = json.loads(resp.body)

    assert resp.status_code == 200
    assert payload == {"manifests": [], "count": 0}


async def test_payload_carries_the_summary_never_prompt_content(app_and_state):
    app, state = app_and_state
    ContextManifestOutbox(state).record(manifest_key("t1", "s1", 0), summary=_summary())

    resp = await _endpoint(app)(_request("task_id=t1&subtask_id=s1"))
    payload = json.loads(resp.body)
    record = payload["manifests"][0]

    # Exactly the summary keys the compiler emits -- the outbox stores no
    # content, so neither can the endpoint.
    assert set(record["summary"]) == {
        "budget_tokens",
        "total_tokens",
        "stable_tokens",
        "risk",
        "over_budget",
        "items_by_tier",
        "dropped",
    }
    assert "content" not in record


async def test_respects_dashboard_password_auth(app_and_state, monkeypatch):
    # The password is read at construction time, so the app must be built
    # after the env is set (the fixture builds one with auth disabled).
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pwd")
    app = DashboardApp(orchestrator=MagicMock())
    endpoint = _endpoint(app)

    resp = await endpoint(
        _request("task_id=t1&subtask_id=s1", host="10.0.0.1")
    )
    assert resp.status_code == 401
