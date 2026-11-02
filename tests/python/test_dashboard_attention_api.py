"""The dashboard attention router must project governance state, never invent it.

The gateway is faked through ``app.state.governance_client``: the contract under
test is the op name + payload on the way out and the JSON shape on the way back.
The hard rule pinned here is that a dead bridge is ``503
{"error": "governance_unavailable"}`` — never a 500 traceback — and that the
human-decision worklist order and the resolve idempotency survive the HTTP hop.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ultimate_coders.dashboard.attention_api import TOKEN_ENV, router

_UNSET = object()

CAPACITY = {
    "worker_slots_total": 8,
    "worker_slots_busy": 5,
    "worker_slots_reserved_human": 1,
    "waiting_event_cases": 2,
    "verifying_cases": 1,
    "needs_human_items": 1,
    "blocked_cases": 1,
    "worker_slots_available": 2,
    "slots_saved_by_quiet_waiting": 3,
}

WAITING = {
    "id": "att-wait",
    "case_id": "case-ci",
    "kind": "waiting_event",
    "summary": "waiting for CI",
    "since": "2026-01-01T00:00:00Z",
    "priority": 0,
}

BLOCKED = {
    "id": "att-blocked",
    "case_id": "case-dep",
    "kind": "blocked",
    "summary": "dependency failed",
    "since": "2026-01-01T00:00:00Z",
    "priority": 9,
}

HUMAN = {
    "id": "att-human",
    "case_id": "case-pick",
    "kind": "needs_human",
    "summary": "pick a branch",
    "detail": "which release?",
    "human_action": "answer",
    "since": "2026-01-01T00:00:00Z",
    "priority": 0,
}

RESOLVED_HUMAN = dict(
    HUMAN,
    resolved_at="2026-01-02T00:00:00Z",
    resolution_summary="human:alice:release/2.0",
)

CASE = {
    "id": "case-1",
    "title": "Ship release 2.0",
    "description": "release the branch",
    "project_id": "proj-1",
    "status": "needs_human",
    "origin": {
        "source": "github",
        "external_ref": "owner/repo#123",
        "thread_key": "owner/repo#123",
    },
    "mandate_id": "mandate-1",
    "runs": [
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "attempt": 1,
            "started_at": "2026-01-01T00:00:00Z",
        }
    ],
    "priority": 3,
    "labels": ["release"],
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "version": 4,
}


class GovernanceUnavailable(Exception):  # noqa: N818 - deliberately mirrors the real name
    """Stand-in for ``ultimate_coders.governance_client.GovernanceUnavailable``."""


class FakeGovernanceClient:
    """Records every op and answers from a per-op table (or raises one error)."""

    def __init__(self, replies: Any = None, *, error: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies = dict(replies or {})
        self._error = error

    async def call(self, op: str, payload: dict[str, Any]) -> Any:
        self.calls.append((op, payload))
        if self._error is not None:
            raise self._error
        reply = self._replies[op]
        return reply(payload) if callable(reply) else reply


def _app(client: Any = _UNSET) -> FastAPI:
    """Build a minimal app around the router, with the governance client injected."""
    app = FastAPI()
    if client is not _UNSET:
        app.state.governance_client = client
    app.include_router(router)
    return app


def _http(app: FastAPI) -> TestClient:
    """A TestClient that surfaces unhandled errors as responses (so 500s are visible)."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_dashboard_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the token gate open; the gate test sets it itself."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)


def test_attention_list_orders_human_first_and_reports_capacity() -> None:
    client = FakeGovernanceClient(
        {"attention.list": {"items": [BLOCKED, WAITING, HUMAN], "capacity": CAPACITY}}
    )

    with _http(_app(client)) as http:
        response = http.get("/api/attention")

    assert response.status_code == 200
    body = response.json()
    # The human decision comes first even though a blocked case has higher priority.
    assert [item["id"] for item in body["items"]] == ["att-human", "att-blocked", "att-wait"]
    assert body["items"][0]["human_action"] == "answer"
    assert body["capacity"]["worker_slots_available"] == 2
    assert body["capacity"]["slots_saved_by_quiet_waiting"] == 3
    assert client.calls == [("attention.list", {"include_resolved": False, "limit": 100})]


def test_router_paths_carry_the_api_prefix() -> None:
    """The router is mounted with no prefix, so every path already starts with /api.

    Pinned because a double prefix (``include_router(router, prefix="/api")``)
    would silently move the whole surface to ``/api/api/...``.
    """
    paths = {route.path for route in router.routes}
    assert paths == {
        "/api/attention",
        "/api/attention/{attention_id}/resolve",
        "/api/capacity",
        "/api/cases",
        "/api/cases/{case_id}",
    }


def test_attention_list_forwards_filters() -> None:
    client = FakeGovernanceClient({"attention.list": {"items": [], "capacity": CAPACITY}})

    with _http(_app(client)) as http:
        response = http.get(
            "/api/attention?include_resolved=true&kind=WAITING_EVENT&case_id=case-ci&limit=7"
        )

    assert response.status_code == 200
    assert client.calls == [
        (
            "attention.list",
            {
                "include_resolved": True,
                "limit": 7,
                "kind": "waiting_event",
                "case_id": "case-ci",
            },
        )
    ]


def test_attention_list_clamps_the_limit() -> None:
    client = FakeGovernanceClient({"attention.list": {"items": [], "capacity": CAPACITY}})

    with _http(_app(client)) as http:
        http.get("/api/attention?limit=9999")
        http.get("/api/attention?limit=-5")

    assert client.calls[0][1]["limit"] == 500
    assert client.calls[1][1]["limit"] == 0


def test_attention_list_rejects_an_unknown_kind_without_calling_the_gateway() -> None:
    client = FakeGovernanceClient({"attention.list": {"items": [], "capacity": CAPACITY}})

    with _http(_app(client)) as http:
        response = http.get("/api/attention?kind=panic")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_kind"}
    assert client.calls == []


def test_attention_list_derives_capacity_when_the_gateway_omits_it() -> None:
    client = FakeGovernanceClient({"attention.list": {"items": [WAITING, HUMAN]}})

    with _http(_app(client)) as http:
        response = http.get("/api/attention")

    assert response.status_code == 200
    capacity = response.json()["capacity"]
    assert capacity["waiting_event_cases"] == 1
    assert capacity["needs_human_items"] == 1
    # The dashboard does not own the worker registry, so slot counts stay zero
    # instead of being guessed.
    assert capacity["worker_slots_total"] == 0
    assert capacity["worker_slots_available"] == 0
    assert capacity["slots_saved_by_quiet_waiting"] == 1


def test_attention_list_skips_an_item_with_an_unknown_kind() -> None:
    future = {
        "id": "att-future",
        "case_id": "case-future",
        "kind": "not_yet_invented",
        "summary": "?",
        "since": "2026-01-01T00:00:00Z",
    }
    client = FakeGovernanceClient(
        {"attention.list": {"items": [HUMAN, future], "capacity": CAPACITY}}
    )

    with _http(_app(client)) as http:
        response = http.get("/api/attention")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["att-human"]


def test_capacity_endpoint_recomputes_the_derived_fields() -> None:
    stale = dict(CAPACITY, worker_slots_available=99, slots_saved_by_quiet_waiting=99)
    client = FakeGovernanceClient({"capacity.get": {"capacity": stale}})

    with _http(_app(client)) as http:
        response = http.get("/api/capacity")

    assert response.status_code == 200
    capacity = response.json()["capacity"]
    assert capacity["worker_slots_available"] == 2
    assert capacity["slots_saved_by_quiet_waiting"] == 3
    assert client.calls == [("capacity.get", {})]


def test_resolve_forwards_the_human_decision_and_returns_the_item() -> None:
    client = FakeGovernanceClient({"attention.resolve": {"item": RESOLVED_HUMAN}})

    with _http(_app(client)) as http:
        response = http.post(
            "/api/attention/att-human/resolve",
            json={
                "resolution_kind": "human_decision",
                "by": "alice",
                "detail": "release/2.0",
            },
        )

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["resolved_at"] == "2026-01-02T00:00:00Z"
    assert item["resolution_summary"] == "human:alice:release/2.0"
    assert client.calls == [
        (
            "attention.resolve",
            {
                "attention_id": "att-human",
                "resolution_kind": "human_decision",
                "principal_kind": "user",
                "principal_id": "alice",
                "by": "alice",
                "detail": "release/2.0",
            },
        )
    ]


def test_resolve_defaults_the_principal_to_the_dashboard() -> None:
    client = FakeGovernanceClient({"attention.resolve": {"item": RESOLVED_HUMAN}})

    with _http(_app(client)) as http:
        response = http.post(
            "/api/attention/att-human/resolve", json={"resolution_kind": "automatic"}
        )

    assert response.status_code == 200
    assert client.calls == [
        (
            "attention.resolve",
            {
                "attention_id": "att-human",
                "resolution_kind": "automatic",
                "principal_kind": "user",
                "principal_id": "dashboard",
            },
        )
    ]


def test_resolve_is_idempotent_across_two_submissions() -> None:
    client = FakeGovernanceClient({"attention.resolve": {"item": RESOLVED_HUMAN}})

    with _http(_app(client)) as http:
        first = http.post(
            "/api/attention/att-human/resolve",
            json={"resolution_kind": "human_decision", "by": "alice"},
        )
        # A second click with a different kind still returns the original
        # resolution: the gateway owns idempotency, the router must not rewrite it.
        second = http.post(
            "/api/attention/att-human/resolve", json={"resolution_kind": "automatic"}
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.json()["item"]["resolution_summary"] == "human:alice:release/2.0"
    assert len(client.calls) == 2


def test_resolve_rejects_an_unknown_resolution_kind() -> None:
    client = FakeGovernanceClient({"attention.resolve": {"item": RESOLVED_HUMAN}})

    with _http(_app(client)) as http:
        response = http.post(
            "/api/attention/att-human/resolve", json={"resolution_kind": "shrug"}
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_resolution_kind"}
    assert client.calls == []


def test_resolve_rejects_a_malformed_body() -> None:
    client = FakeGovernanceClient({"attention.resolve": {"item": RESOLVED_HUMAN}})

    with _http(_app(client)) as http:
        bad_json = http.post(
            "/api/attention/att-human/resolve",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        not_an_object = http.post("/api/attention/att-human/resolve", json=[])
        bad_by = http.post(
            "/api/attention/att-human/resolve",
            json={"resolution_kind": "automatic", "by": 7},
        )

    assert bad_json.status_code == 400
    assert bad_json.json() == {"error": "invalid_body"}
    assert not_an_object.status_code == 400
    assert not_an_object.json() == {"error": "invalid_body"}
    assert bad_by.status_code == 400
    assert bad_by.json() == {"error": "invalid_by"}
    assert client.calls == []


def test_resolve_requires_the_dashboard_token_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    client = FakeGovernanceClient({"attention.resolve": {"item": RESOLVED_HUMAN}})
    body = {"resolution_kind": "human_decision", "by": "alice"}

    with _http(_app(client)) as http:
        missing = http.post("/api/attention/att-human/resolve", json=body)
        wrong = http.post(
            "/api/attention/att-human/resolve",
            json=body,
            headers={"Authorization": "Bearer nope"},
        )
        ok = http.post(
            "/api/attention/att-human/resolve",
            json=body,
            headers={"Authorization": "Bearer s3cret"},
        )

    assert missing.status_code == 401
    assert missing.json() == {"error": "unauthorized"}
    assert wrong.status_code == 401
    assert ok.status_code == 200
    # A rejected request must never reach the gateway.
    assert len(client.calls) == 1


def test_resolve_is_open_when_no_token_is_configured() -> None:
    client = FakeGovernanceClient({"attention.resolve": {"item": RESOLVED_HUMAN}})

    with _http(_app(client)) as http:
        response = http.post(
            "/api/attention/att-human/resolve", json={"resolution_kind": "automatic"}
        )

    assert response.status_code == 200


def test_resolve_reports_a_missing_item_as_404() -> None:
    client = FakeGovernanceClient({"attention.resolve": {}})

    with _http(_app(client)) as http:
        response = http.post(
            "/api/attention/att-missing/resolve", json={"resolution_kind": "automatic"}
        )

    assert response.status_code == 404
    assert response.json() == {"error": "attention_not_found"}


def test_resolve_reports_an_unusable_item_as_502() -> None:
    client = FakeGovernanceClient({"attention.resolve": {"item": {"id": "att-human"}}})

    with _http(_app(client)) as http:
        response = http.post(
            "/api/attention/att-human/resolve", json={"resolution_kind": "automatic"}
        )

    assert response.status_code == 502
    assert response.json() == {"error": "invalid_gateway_reply"}


def test_cases_list_forwards_filters_and_returns_the_total() -> None:
    client = FakeGovernanceClient({"case.list": {"cases": [CASE], "total": 1}})

    with _http(_app(client)) as http:
        response = http.get(
            "/api/cases?status=needs_human&project_id=proj-1"
            "&thread_key=owner/repo%23123&include_terminal=true&limit=10"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["cases"] == [CASE]
    assert body["total"] == 1
    assert client.calls == [
        (
            "case.list",
            {
                "include_terminal": True,
                "limit": 10,
                "status": "needs_human",
                "project_id": "proj-1",
                "thread_key": "owner/repo#123",
            },
        )
    ]


def test_cases_list_rejects_an_unknown_status() -> None:
    client = FakeGovernanceClient({"case.list": {"cases": [], "total": 0}})

    with _http(_app(client)) as http:
        response = http.get("/api/cases?status=finished")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_status"}
    assert client.calls == []


def test_cases_list_tolerates_a_partial_reply() -> None:
    client = FakeGovernanceClient(
        {
            "case.list": lambda payload: {"cases": [CASE]}
            if payload["limit"] == 10
            else {"cases": "junk"}
        }
    )

    with _http(_app(client)) as http:
        partial = http.get("/api/cases?limit=10")
        unusable = http.get("/api/cases?limit=0")

    assert partial.status_code == 200
    assert partial.json()["total"] == 1
    assert unusable.status_code == 200
    assert unusable.json() == {"cases": [], "total": 0}


def test_cases_get_returns_the_case() -> None:
    client = FakeGovernanceClient({"case.get": {"case": CASE}})

    with _http(_app(client)) as http:
        response = http.get("/api/cases/case-1")

    assert response.status_code == 200
    assert response.json() == {"case": CASE}
    assert client.calls == [("case.get", {"case_id": "case-1"})]


def test_cases_get_reports_a_missing_case_as_404() -> None:
    client = FakeGovernanceClient({"case.get": {}})

    with _http(_app(client)) as http:
        response = http.get("/api/cases/case-missing")

    assert response.status_code == 404
    assert response.json() == {"error": "case_not_found"}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/attention", None),
        ("get", "/api/capacity", None),
        ("get", "/api/cases", None),
        ("get", "/api/cases/case-1", None),
        ("post", "/api/attention/att-human/resolve", {"resolution_kind": "automatic"}),
    ],
)
def test_a_dead_bridge_is_503_not_500(method: str, path: str, body: Any) -> None:
    client = FakeGovernanceClient(error=GovernanceUnavailable("nats is down"))

    with _http(_app(client)) as http:
        call = getattr(http, method)
        response = call(path) if body is None else call(path, json=body)

    assert response.status_code == 503
    assert response.json() == {"error": "governance_unavailable"}


def test_the_lead_owned_unavailable_type_is_recognised_by_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real client module raises its own class; the router must match by type.

    ``ultimate_coders.governance_client`` is wired by the Lead, so this test
    stands in for it with a module of the same name whose exception is
    deliberately called something else: only the ``isinstance`` path can map it
    to 503 (the class-name fallback cannot see it).
    """
    gateway_module = types.ModuleType("ultimate_coders.governance_client")

    class BridgeDownError(Exception):
        """Name does not matter; the router imports the class, it does not guess."""

    gateway_module.GovernanceUnavailable = BridgeDownError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ultimate_coders.governance_client", gateway_module)
    client = FakeGovernanceClient(error=BridgeDownError("bridge is down"))

    with _http(_app(client)) as http:
        response = http.get("/api/capacity")

    assert response.status_code == 503
    assert response.json() == {"error": "governance_unavailable"}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/attention", None),
        ("get", "/api/capacity", None),
        ("get", "/api/cases", None),
        ("get", "/api/cases/case-1", None),
        ("post", "/api/attention/att-human/resolve", {"resolution_kind": "automatic"}),
    ],
)
def test_a_missing_governance_client_is_503_not_500(
    method: str, path: str, body: Any
) -> None:
    with _http(_app()) as http:
        call = getattr(http, method)
        response = call(path) if body is None else call(path, json=body)

    assert response.status_code == 503
    assert response.json() == {"error": "governance_unavailable"}
