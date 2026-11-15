"""Workflow replay aggregation and the dashboard endpoint that serves it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from ultimate_coders.dashboard.app import DashboardApp
from ultimate_coders.dashboard.workflows import (
    MAX_REPLAY_RUNS,
    MAX_REPLAY_STEPS,
    build_workflow_replays,
)


@pytest.fixture(autouse=True)
def _dashboard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DASHBOARD_PASSWORD",
        "UC_DASHBOARD_TOKEN",
        "UC_DASHBOARD_TRUST_LOCALHOST",
        "UC_DASHBOARD_ALLOW_OPEN_MUTATIONS",
    ):
        monkeypatch.delenv(name, raising=False)


def _event(
    task_id: str,
    subtask_id: str,
    step_index: int | str,
    step_status: str,
    timestamp: str,
    *,
    step_total: int = 3,
    step_agent: str = "codex",
    step_summary: str = "",
    percent: int = 0,
    event_type: str = "subtask_progress",
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": event_type,
        "task_id": task_id,
        "subtask_id": subtask_id,
        "data": {
            "subtask_id": subtask_id,
            "step_index": step_index,
            "step_total": step_total,
            "step_agent": step_agent,
            "step_status": step_status,
            "step_summary": step_summary,
            "percent": percent,
        },
    }


def test_replays_group_by_run_keep_step_order_and_latest_summary():
    events = [
        # Newest run first in the input, as durable history already returns it.
        _event("task-b", "s-b", 0, "completed", "2026-11-15T12:05:00+00:00", step_total=1),
        _event(
            "task-a",
            "s-a",
            1,
            "failed",
            "2026-11-15T12:02:30+00:00",
            step_total=2,
            step_summary="step two failed",
        ),
        _event("task-a", "s-a", 0, "completed", "2026-11-15T12:01:00+00:00", step_total=2),
        _event("task-a", "s-a", 0, "started", "2026-11-15T12:00:00+00:00", step_total=2),
        _event("task-a", "s-a", 1, "started", "2026-11-15T12:02:00+00:00", step_total=2),
    ]

    replays = build_workflow_replays(events)

    assert [run["id"] for run in replays] == ["task-b:s-b", "task-a:s-a"]
    failing = replays[1]
    assert failing["status"] == "failed"
    assert failing["step_total"] == 2
    assert [step["index"] for step in failing["steps"]] == [0, 1]
    assert failing["steps"][0]["status"] == "completed"
    assert failing["steps"][0]["duration_ms"] == 60_000
    assert failing["steps"][1]["status"] == "failed"
    assert failing["steps"][1]["summary"] == "step two failed"
    assert failing["steps"][1]["duration_ms"] == 30_000


def test_replays_ignore_malformed_and_non_step_events():
    events = [
        {"type": "subtask_progress", "task_id": "t", "subtask_id": "s", "data": {}},
        {
            "type": "subtask_progress",
            "task_id": "t",
            "subtask_id": "s",
            "data": {"step_index": 0, "step_status": "bogus"},
        },
        _event("t", "s", 0, "completed", "not-a-timestamp"),
        _event(
            "t",
            "s",
            1,
            "completed",
            "2026-11-15T12:00:00+00:00",
            event_type="subtask_completed",
        ),
        None,  # type: ignore[list-item]
    ]

    replays = build_workflow_replays(events)

    assert len(replays) == 1
    assert replays[0]["id"] == "t:s"
    assert replays[0]["steps"][0]["started_at"] is None


def test_replays_bound_runs_and_steps():
    events = []
    for run in range(3):
        for step in range(4):
            events.append(
                _event(
                    f"task-{run}",
                    f"sub-{run}",
                    step,
                    "completed",
                    f"2026-11-15T12:0{step}:00+00:00",
                    step_total=4,
                )
            )

    replays = build_workflow_replays(events, max_runs=2, max_steps=2)

    assert len(replays) == 2
    assert all(run["step_count"] == 2 for run in replays)
    assert all(run["status"] == "completed" for run in replays)
    assert MAX_REPLAY_RUNS >= 2
    assert MAX_REPLAY_STEPS >= 2


def test_replays_accept_details_key_and_running_status():
    events = [
        {
            "timestamp": "2026-11-15T12:00:00+00:00",
            "type": "subtask_progress",
            "task_id": "task-x",
            "details": {"step_index": "0", "step_total": "2", "step_status": "started"},
        }
    ]

    replays = build_workflow_replays(events)

    assert len(replays) == 1
    assert replays[0]["status"] == "running"
    assert replays[0]["steps"][0]["index"] == 0


def test_replay_endpoint_reads_the_local_event_log():
    app = DashboardApp(orchestrator=None)
    app._event_log.appendleft(
        _event("task-rest", "sub-rest", 0, "completed", "2026-11-15T12:00:00+00:00")
    )

    response = TestClient(app._app).get("/dashboard/api/workflows/replays")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["source"] == "local"
    assert body["runs"][0]["id"] == "task-rest:sub-rest"
    assert body["runs"][0]["steps"][0]["status"] == "completed"


def test_replay_endpoint_prefers_durable_history(monkeypatch: pytest.MonkeyPatch):
    app = DashboardApp(orchestrator=None, nats_client=object())
    app._event_log.appendleft(
        _event("task-local", "sub-local", 0, "completed", "2026-11-15T12:00:00+00:00")
    )
    history_event = _event(
        "task-nats", "sub-nats", 0, "completed", "2026-11-15T13:00:00+00:00"
    )

    async def fake_history(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"available": True, "events": [history_event], "total": 1}

    monkeypatch.setattr(
        "ultimate_coders.dashboard.app.list_event_history", fake_history
    )

    body = TestClient(app._app).get("/dashboard/api/workflows/replays").json()

    assert body["source"] == "nats"
    assert [run["id"] for run in body["runs"]] == ["task-nats:sub-nats"]


def test_replay_endpoint_obeys_the_dashboard_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UC_DASHBOARD_TOKEN", "replay-secret")
    app = DashboardApp(orchestrator=None)
    app._event_log.appendleft(
        _event("task-auth", "sub-auth", 0, "completed", "2026-11-15T12:00:00+00:00")
    )
    client = TestClient(app._app, client=("203.0.113.10", 1234))

    assert client.get("/dashboard/api/workflows/replays").status_code == 401
    authorized = client.get(
        "/dashboard/api/workflows/replays",
        headers={"Authorization": "Bearer replay-secret"},
    )
    assert authorized.status_code == 200
    assert authorized.json()["runs"][0]["task_id"] == "task-auth"
