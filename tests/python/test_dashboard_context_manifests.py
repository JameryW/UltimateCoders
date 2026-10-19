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


async def test_requires_task_id(app_and_state):
    app, _state = app_and_state
    endpoint = _endpoint(app)

    for query in ("", "subtask_id=s1", "limit=10"):
        resp = await endpoint(_request(query))
        assert resp.status_code == 400, query
        assert "task_id is required" in resp.body.decode()


async def test_task_only_query_spans_every_subtask(app_and_state):
    app, state = app_and_state
    outbox = ContextManifestOutbox(state)
    outbox.record(manifest_key("t1", "s1", 0), summary=_summary(100))
    outbox.record(manifest_key("t1", "s2", 0), summary=_summary(200))
    outbox.record(manifest_key("t1", "s2", 1), summary=_summary(300))
    outbox.record(manifest_key("t-other", "s1", 0), summary=_summary(400))

    resp = await _endpoint(app)(_request("task_id=t1"))
    payload = json.loads(resp.body)

    assert resp.status_code == 200
    assert payload["count"] == 3
    assert [m["key"] for m in payload["manifests"]] == [
        "t1:s1:0",
        "t1:s2:0",
        "t1:s2:1",
    ]


async def test_task_scan_sorts_attempts_numerically(app_and_state):
    # Plain key order would put ":10" before ":2"; the endpoint re-sorts so
    # "what did attempt 2 look like" does not require knowing that.
    app, state = app_and_state
    outbox = ContextManifestOutbox(state)
    for attempt in (0, 2, 10):
        outbox.record(manifest_key("t1", "s1", attempt), summary=_summary(attempt))

    payload = json.loads((await _endpoint(app)(_request("task_id=t1"))).body)

    assert [m["key"] for m in payload["manifests"]] == [
        "t1:s1:0",
        "t1:s1:2",
        "t1:s1:10",
    ]


async def test_task_prefix_does_not_swallow_a_longer_task_id(app_and_state):
    app, state = app_and_state
    outbox = ContextManifestOutbox(state)
    outbox.record(manifest_key("task-1", "s1", 0), summary=_summary(100))
    outbox.record(manifest_key("task-10", "s1", 0), summary=_summary(200))

    payload = json.loads((await _endpoint(app)(_request("task_id=task-1"))).body)

    assert [m["key"] for m in payload["manifests"]] == ["task-1:s1:0"]


async def test_unknown_task_yields_an_empty_scan(app_and_state):
    app, _state = app_and_state

    payload = json.loads((await _endpoint(app)(_request("task_id=nope"))).body)

    assert payload == {"manifests": [], "count": 0}


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


async def test_order_recent_is_a_time_window_not_a_key_window(app_and_state, monkeypatch):
    # Same honesty pin as ContextManifestOutbox.recent, one layer up: the
    # newest compile holds the LAST key, so under limit=1 an endpoint that
    # truncated in key order (or sorted after a key-limited fetch) could
    # never return it.
    app, state = app_and_state
    outbox = ContextManifestOutbox(state)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "ultimate_coders.context_outbox.time.time", lambda: clock["now"]
    )
    for subtask, budget, at in (("a", 100, 1000.0), ("z", 300, 3000.0)):
        clock["now"] = at
        outbox.record(manifest_key("t1", subtask, 0), summary=_summary(budget))

    payload = json.loads(
        (await _endpoint(app)(_request("task_id=t1&order=recent&limit=1"))).body
    )
    assert [m["key"] for m in payload["manifests"]] == ["t1:z:0"]

    payload = json.loads(
        (await _endpoint(app)(_request("task_id=t1&order=recent"))).body
    )
    assert [m["key"] for m in payload["manifests"]] == ["t1:z:0", "t1:a:0"]


async def test_order_recent_respects_the_subtask_scope(app_and_state, monkeypatch):
    app, state = app_and_state
    outbox = ContextManifestOutbox(state)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "ultimate_coders.context_outbox.time.time", lambda: clock["now"]
    )
    for key, budget, at in (
        (manifest_key("t1", "s1", 0), 100, 1000.0),
        (manifest_key("t1", "s2", 0), 200, 2000.0),
    ):
        clock["now"] = at
        outbox.record(key, summary=_summary(budget))

    payload = json.loads(
        (await _endpoint(app)(_request("task_id=t1&subtask_id=s1&order=recent"))).body
    )
    assert [m["key"] for m in payload["manifests"]] == ["t1:s1:0"]


async def test_rejects_unknown_order_value(app_and_state):
    app, _state = app_and_state

    for order in ("recorded_at", "RECENT", "anything"):
        resp = await _endpoint(app)(_request(f"task_id=t1&order={order}"))
        assert resp.status_code == 400, order
        assert b"Invalid order" in resp.body


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
