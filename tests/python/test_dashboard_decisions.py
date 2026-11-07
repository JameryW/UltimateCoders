"""Dashboard read surface for Decision Records (V8 P0).

Mirrors the auth / task_id / limit guards of context manifests. Response
shape is ``{decisions, count, total}`` — never prompt content.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from ultimate_coders.dashboard.app import DashboardApp
from ultimate_coders.decision_record import DecisionRecordOutbox, decision_key
from ultimate_coders.runtime_state import RuntimeState


def _request(query: str = "", host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/dashboard/api/decisions",
            "query_string": query.encode(),
            "headers": [],
            "client": (host, 12345),
        }
    )


def _endpoint(app: DashboardApp):
    for route in app._app.routes:
        if getattr(route, "path", None) == "/dashboard/api/decisions":
            return route.endpoint
    raise AssertionError("decisions route not registered")


@pytest.fixture
def app_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = DashboardApp(orchestrator=MagicMock())
    return app, RuntimeState()


def _seed(outbox: DecisionRecordOutbox, key: str, *, worker_id: str = "w") -> None:
    outbox.record_start(key, worker_id=worker_id)
    outbox.record_terminal(key, success=True, summary="ok")


async def test_requires_task_id(app_and_state):
    app, _state = app_and_state
    endpoint = _endpoint(app)

    for query in ("", "subtask_id=s1", "limit=10"):
        resp = await endpoint(_request(query))
        assert resp.status_code == 400, query
        assert "task_id is required" in resp.body.decode()


async def test_task_only_query_spans_every_subtask(app_and_state):
    app, state = app_and_state
    outbox = DecisionRecordOutbox(state)
    _seed(outbox, decision_key("t1", "s1", 0))
    _seed(outbox, decision_key("t1", "s2", 0))
    _seed(outbox, decision_key("t1", "s2", 1))
    _seed(outbox, decision_key("t-other", "s1", 0))

    resp = await _endpoint(app)(_request("task_id=t1"))
    payload = json.loads(resp.body)

    assert resp.status_code == 200
    assert payload["count"] == 3
    assert [d["key"] for d in payload["decisions"]] == [
        "t1:s1:0",
        "t1:s2:0",
        "t1:s2:1",
    ]


async def test_task_scan_sorts_attempts_numerically(app_and_state):
    app, state = app_and_state
    outbox = DecisionRecordOutbox(state)
    for attempt in (0, 2, 10):
        _seed(outbox, decision_key("t1", "s1", attempt), worker_id=f"w{attempt}")

    payload = json.loads((await _endpoint(app)(_request("task_id=t1"))).body)

    assert [d["key"] for d in payload["decisions"]] == [
        "t1:s1:0",
        "t1:s1:2",
        "t1:s1:10",
    ]


async def test_task_prefix_does_not_swallow_a_longer_task_id(app_and_state):
    app, state = app_and_state
    outbox = DecisionRecordOutbox(state)
    _seed(outbox, decision_key("task-1", "s1", 0))
    _seed(outbox, decision_key("task-10", "s1", 0))

    payload = json.loads((await _endpoint(app)(_request("task_id=task-1"))).body)

    assert [d["key"] for d in payload["decisions"]] == ["task-1:s1:0"]


async def test_unknown_task_yields_an_empty_scan(app_and_state):
    app, _state = app_and_state

    payload = json.loads((await _endpoint(app)(_request("task_id=nope"))).body)

    assert payload == {"decisions": [], "count": 0, "total": 0}


async def test_rejects_unbounded_or_non_numeric_limit(app_and_state):
    app, _state = app_and_state
    endpoint = _endpoint(app)

    for limit in ("0", "1001", "abc"):
        resp = await endpoint(_request(f"task_id=t1&subtask_id=s1&limit={limit}"))
        assert resp.status_code == 400, limit


async def test_returns_the_subtask_history_in_key_order(app_and_state):
    app, state = app_and_state
    outbox = DecisionRecordOutbox(state)
    _seed(outbox, decision_key("t1", "s1", 0), worker_id="w0")
    _seed(outbox, decision_key("t1", "s1", 1), worker_id="w1")
    _seed(outbox, decision_key("t1", "s2", 0), worker_id="w2")

    resp = await _endpoint(app)(_request("task_id=t1&subtask_id=s1"))
    payload = json.loads(resp.body)

    assert resp.status_code == 200
    assert payload["count"] == 2
    assert [d["key"] for d in payload["decisions"]] == ["t1:s1:0", "t1:s1:1"]
    assert payload["decisions"][0]["worker_id"] == "w0"
    assert payload["decisions"][1]["terminal"]["success"] is True


async def test_unknown_subtask_yields_an_empty_history_not_an_error(app_and_state):
    app, _state = app_and_state

    resp = await _endpoint(app)(_request("task_id=t1&subtask_id=nope"))
    payload = json.loads(resp.body)

    assert resp.status_code == 200
    assert payload == {"decisions": [], "count": 0, "total": 0}


async def test_total_is_the_scan_count_not_the_window(app_and_state):
    app, state = app_and_state
    outbox = DecisionRecordOutbox(state)
    for attempt in (0, 1, 2):
        _seed(outbox, decision_key("t1", "s1", attempt))

    payload = json.loads(
        (await _endpoint(app)(_request("task_id=t1&limit=1"))).body
    )
    assert payload["count"] == 1
    assert payload["total"] == 3

    payload = json.loads((await _endpoint(app)(_request("task_id=t1"))).body)
    assert payload["count"] == 3
    assert payload["total"] == 3


async def test_total_respects_the_subtask_scope(app_and_state):
    app, state = app_and_state
    outbox = DecisionRecordOutbox(state)
    _seed(outbox, decision_key("t1", "s1", 0))
    _seed(outbox, decision_key("t1", "s2", 0))

    payload = json.loads(
        (await _endpoint(app)(_request("task_id=t1&subtask_id=s1"))).body
    )
    assert payload["total"] == 1


async def test_payload_never_includes_prompt_content(app_and_state):
    app, state = app_and_state
    outbox = DecisionRecordOutbox(state)
    outbox.record_start(
        decision_key("t1", "s1", 0),
        worker_id="w",
        context_digest="abc",
        context_key="t1:s1:0",
    )
    outbox.record_terminal(
        decision_key("t1", "s1", 0),
        success=True,
        summary="ok",
    )

    resp = await _endpoint(app)(_request("task_id=t1&subtask_id=s1"))
    payload = json.loads(resp.body)
    record = payload["decisions"][0]
    blob = json.dumps(record)

    assert "content" not in record
    assert "prompt" not in blob
    assert record["context_digest"] == "abc"
    assert record["terminal"]["summary"] == "ok"


async def test_respects_dashboard_password_auth(app_and_state, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pwd")
    app = DashboardApp(orchestrator=MagicMock())
    endpoint = _endpoint(app)

    resp = await endpoint(_request("task_id=t1&subtask_id=s1", host="10.0.0.1"))
    assert resp.status_code == 401


async def test_unavailable_state_maps_to_503(app_and_state, monkeypatch):
    """Ticket 02: same unavailable helper pattern as context manifests."""
    app, _state = app_and_state

    def _boom(*args, **kwargs):
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr(RuntimeState, "query", _boom)

    resp = await _endpoint(app)(_request("task_id=t1&subtask_id=s1"))
    assert resp.status_code == 503
    payload = json.loads(resp.body)
    assert "unavailable" in payload["error"].lower()
