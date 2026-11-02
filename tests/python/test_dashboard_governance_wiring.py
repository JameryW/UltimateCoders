"""Wiring test: the dashboard must actually mount the governance routers.

This is the integration seam between `DashboardApp` and the three PR04/PR05/PR06
FastAPI routers. It exists because a router can be perfect and still be dead code:
the failure mode this test rules out is "the module was written but never
included", which no unit test of the router can catch.

It also pins the *degradation* rule: with no governance bridge connected, the
governance endpoints answer 503 ``governance_unavailable`` — never 500, and never
a locally invented payload.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from ultimate_coders.dashboard.app import DashboardApp
from ultimate_coders.dashboard.governance_routes import GOVERNANCE_ROUTER_MODULES


@pytest.fixture()
def dashboard() -> DashboardApp:
    return DashboardApp(None)


def test_all_three_governance_routers_are_importable() -> None:
    assert GOVERNANCE_ROUTER_MODULES == (
        "ultimate_coders.dashboard.trigger_api",
        "ultimate_coders.dashboard.attention_api",
        "ultimate_coders.dashboard.proactive_api",
    )
    from ultimate_coders.dashboard.governance_routes import governance_routers

    routers = governance_routers()
    assert len(routers) == 3, "a governance router failed to import or expose `router`"
    paths: list[str] = []
    for router in routers:
        paths.extend(getattr(route, "path", "") for route in router.routes)
    for expected in (
        "/api/attention",
        "/api/capacity",
        "/api/cases",
        "/api/triggers/github",
        "/api/proactive/state",
        "/api/watchdog/stall",
    ):
        assert expected in paths, f"{expected} is not registered on any governance router"


def test_dashboard_exposes_the_governance_client_on_app_state(dashboard: DashboardApp) -> None:
    assert hasattr(dashboard._app.state, "governance_client")
    assert dashboard._app.state.governance_client is dashboard._governance_client


def test_governance_endpoints_degrade_to_503_without_a_bridge(dashboard: DashboardApp) -> None:
    client = TestClient(dashboard._app)
    for path in (
        "/api/attention",
        "/api/capacity",
        "/api/cases",
        "/api/triggers/subscriptions",
        "/api/proactive/state",
        "/api/proactive/proposals",
        "/api/proactive/audit",
    ):
        response = client.get(path)
        assert response.status_code == 503, f"{path} returned {response.status_code}"
        assert response.json() == {"error": "governance_unavailable"}


def test_governance_endpoints_proxy_the_gateway_answer(dashboard: DashboardApp) -> None:
    class FakeClient:
        async def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
            if op == "capacity.get":
                return {
                    "capacity": {
                        "worker_slots_total": 4,
                        "worker_slots_busy": 1,
                        "waiting_event_cases": 3,
                        "verifying_cases": 0,
                    }
                }
            if op == "attention.list":
                return {
                    "items": [
                        {
                            "id": "att-1",
                            "case_id": "case-1",
                            "kind": "needs_human",
                            "summary": "pick a branch",
                            "since": 1_700_000_000,
                        }
                    ],
                    "capacity": {"worker_slots_total": 4},
                }
            return {}

    dashboard._app.state.governance_client = FakeClient()
    http = TestClient(dashboard._app)

    capacity = http.get("/api/capacity")
    assert capacity.status_code == 200
    body = capacity.json()["capacity"]
    assert body["worker_slots_total"] == 4
    assert body["slots_saved_by_quiet_waiting"] == 3

    attention = http.get("/api/attention")
    assert attention.status_code == 200
    items = attention.json()["items"]
    assert [item["kind"] for item in items] == ["needs_human"]
