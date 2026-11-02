"""Dashboard proactive API: a transport for the gateway, never a second authority.

The acceptance pinned here:

* every endpoint forwards the documented governance op and payload;
* a refused proposal keeps the gateway's ``deny_code``/``deny_detail`` (the
  false trigger is auditable) instead of being re-judged locally;
* an unknown mode/kind/decision is rejected on shape, so a typo cannot be
  coerced into a valid request;
* a missing client or an unavailable bridge is ``503``
  ``{"error": "governance_unavailable"}`` -- never a 500 traceback and never a
  fabricated empty state -- while an unexpected bug is *not* masked as 503;
* the watchdog endpoint forwards the canonical stall report and the returned
  verdict maps to the orchestrator action, escalating when replans are spent.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from ultimate_coders import governance_client as transport
from ultimate_coders.dashboard import proactive_api
from ultimate_coders.dashboard.proactive_api import GovernanceUnavailable, build_router
from ultimate_coders.governance_client import GovernanceError
from ultimate_coders.proactive.watchdog_input import apply_verdict

_API_LOGGER = "ultimate_coders.dashboard.proactive_api"

_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/api/proactive/state", None),
    ("post", "/api/proactive/mode", {"mode": "observe"}),
    ("get", "/api/proactive/proposals", None),
    (
        "post",
        "/api/proactive/proposals",
        {
            "kind": "fix_failing_ci",
            "title": "Fix CI",
            "rationale": "run failed",
            "confidence": 0.6,
            "evidence": ["ci:run-42"],
        },
    ),
    ("post", "/api/proactive/proposals/p1/decide", {"decision": "approve"}),
    ("get", "/api/proactive/audit", None),
    ("post", "/api/watchdog/stall", {"task_id": "task-1", "no_progress_count": 2}),
]

_VALID_PROPOSAL: dict[str, Any] = {
    "kind": "fix_failing_ci",
    "title": "Fix failing CI run run-42",
    "rationale": "CI run run-42 concluded failure",
    "confidence": 0.6,
    "evidence": ["ci:run-42"],
    "proposed_request": "Investigate and fix the failing CI run run-42.",
    "project_id": "proj",
    "estimated_tokens": 40000,
    "case_id": "case-1",
}


class FakeGovernance:
    """Stand-in for ``GovernanceClient`` exposing the same ``call`` coroutine."""

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}
        self._error = error

    async def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, dict(payload)))
        if self._error is not None:
            raise self._error
        response = self._responses.get(op, {})
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(payload)
        return response


def _client(governance: FakeGovernance | None = None, *, auth: Any = None) -> TestClient:
    app = FastAPI()
    if governance is not None:
        app.state.governance_client = governance
    app.include_router(build_router(auth=auth))
    return TestClient(app, raise_server_exceptions=False)


# ── state / mode ─────────────────────────────────────────────


def test_state_forwards_the_op_and_returns_the_gateway_body() -> None:
    gateway_state = {
        "state": {
            "mode": "off",
            "max_open_proposals": 5,
            "always_require_approval": False,
            "open_proposals": 0,
            "evaluated_count": 0,
            "approved_count": 0,
            "rejected_count": 0,
            "executed_count": 0,
        }
    }
    fake = FakeGovernance({"proactive.state.get": gateway_state})
    with _client(fake) as client:
        response = client.get("/api/proactive/state")
    assert response.status_code == 200
    assert response.json() == gateway_state
    assert fake.calls == [("proactive.state.get", {})]


def test_mode_set_forwards_the_full_payload() -> None:
    fake = FakeGovernance({"proactive.mode.set": {"state": {"mode": "propose"}}})
    body = {
        "mode": "propose",
        "max_open_proposals": 3,
        "max_tokens_per_action": 50000,
        "always_require_approval": True,
        "reason": "operator opt-in",
        "principal_kind": "user",
        "principal_id": "alice",
    }
    with _client(fake) as client:
        response = client.post("/api/proactive/mode", json=body)
    assert response.status_code == 200
    assert fake.calls == [("proactive.mode.set", body)]
    assert response.json()["state"]["mode"] == "propose"


def test_mode_set_defaults_the_principal() -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        client.post("/api/proactive/mode", json={"mode": "off"})
    assert fake.calls[0][1] == {
        "mode": "off",
        "principal_kind": proactive_api.DEFAULT_PRINCIPAL_KIND,
        "principal_id": proactive_api.DEFAULT_PRINCIPAL_ID,
    }


@pytest.mark.parametrize("mode", ["off", "observe", "propose", "execute"])
def test_mode_set_accepts_every_canonical_token(mode: str) -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        assert client.post("/api/proactive/mode", json={"mode": mode}).status_code == 200
    assert fake.calls[0][1]["mode"] == mode


@pytest.mark.parametrize("mode", ["proposee", "", "on", "true", 3])
def test_mode_set_rejects_an_unknown_token_without_calling_the_gateway(mode: object) -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        response = client.post("/api/proactive/mode", json={"mode": mode})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_mode"
    assert fake.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "off", "max_open_proposals": "3"},
        {"mode": "off", "max_open_proposals": -1},
        {"mode": "off", "max_tokens_per_action": 1.5},
        {"mode": "off", "always_require_approval": "yes"},
        {"mode": "off", "reason": 7},
        {"mode": "off", "principal_id": ""},
    ],
)
def test_mode_set_rejects_malformed_fields(body: dict[str, Any]) -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        response = client.post("/api/proactive/mode", json=body)
    assert response.status_code == 400
    assert fake.calls == []


# ── proposals ────────────────────────────────────────────────


def test_proposals_list_forwards_filters_and_defaults_the_limit() -> None:
    fake = FakeGovernance({"proactive.proposal.list": {"proposals": []}})
    with _client(fake) as client:
        response = client.get(
            "/api/proactive/proposals",
            params={"status": "proposed", "case_id": "case-1", "limit": "5"},
        )
        client.get("/api/proactive/proposals")
    assert response.status_code == 200
    assert fake.calls[0] == (
        "proactive.proposal.list",
        {"limit": 5, "status": "proposed", "case_id": "case-1"},
    )
    assert fake.calls[1][1] == {"limit": proactive_api.DEFAULT_LIST_LIMIT}


@pytest.mark.parametrize(
    "params",
    [{"status": "proposedd"}, {"limit": "0"}, {"limit": "501"}, {"limit": "abc"}],
)
def test_proposals_list_rejects_bad_query_parameters(params: dict[str, str]) -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        response = client.get("/api/proactive/proposals", params=params)
    assert response.status_code == 400
    assert fake.calls == []


def test_proposal_submit_forwards_the_gateway_payload() -> None:
    accepted = {"accepted": True, "proposal": {"id": "p1", "status": "proposed"}}
    fake = FakeGovernance({"proactive.proposal.submit": accepted})
    with _client(fake) as client:
        response = client.post("/api/proactive/proposals", json=_VALID_PROPOSAL)
    assert response.status_code == 200
    assert response.json() == accepted
    op, payload = fake.calls[0]
    assert op == "proactive.proposal.submit"
    assert payload == {
        **_VALID_PROPOSAL,
        "principal_kind": proactive_api.DEFAULT_PRINCIPAL_KIND,
        "principal_id": proactive_api.DEFAULT_PRINCIPAL_ID,
    }


def test_empty_evidence_is_forwarded_so_the_gateway_can_refuse_it() -> None:
    """The evidence rule belongs to the control plane; this layer must not re-judge it."""
    fake = FakeGovernance({"proactive.proposal.submit": {"accepted": True, "proposal": {}}})
    with _client(fake) as client:
        response = client.post(
            "/api/proactive/proposals",
            json={**_VALID_PROPOSAL, "evidence": []},
        )
    assert response.status_code == 200
    assert fake.calls[0][1]["evidence"] == []


def test_a_refused_proposal_keeps_the_deny_reason_and_is_auditable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit: list[dict[str, Any]] = []

    def submit(payload: dict[str, Any]) -> dict[str, Any]:
        audit.append(
            {
                "at": "2026-01-01T00:00:00Z",
                "mode": "propose",
                "action": "admit_proposal",
                "allowed": False,
                "reason": "proactive_mode_disallows",
                "detail": "propose_without_evidence",
                "evidence": payload["evidence"],
            }
        )
        return {
            "accepted": False,
            "deny_code": "proactive_mode_disallows",
            "deny_detail": "propose_without_evidence",
        }

    fake = FakeGovernance(
        {
            "proactive.proposal.submit": submit,
            "proactive.audit.list": lambda payload: {"entries": list(audit)},
        }
    )
    with _client(fake) as client:
        with caplog.at_level(logging.WARNING, logger=_API_LOGGER):
            denied = client.post(
                "/api/proactive/proposals", json={**_VALID_PROPOSAL, "evidence": []}
            )
        trail = client.get("/api/proactive/audit", params={"limit": "10"})

    assert denied.status_code == 200
    body = denied.json()
    assert body["accepted"] is False
    assert body["deny_code"] == "proactive_mode_disallows"
    assert body["deny_detail"] == "propose_without_evidence"
    assert any("propose_without_evidence" in record.getMessage() for record in caplog.records)

    assert trail.status_code == 200
    entry = trail.json()["entries"][0]
    assert entry["allowed"] is False
    assert entry["reason"] == body["deny_code"]
    assert entry["detail"] == body["deny_detail"]
    assert entry["evidence"] == []


@pytest.mark.parametrize(
    "override, error",
    [
        ({"kind": "make_coffee"}, "invalid_kind"),
        ({"title": "  "}, "invalid_title"),
        ({"rationale": ""}, "invalid_rationale"),
        ({"confidence": 1.5}, "invalid_confidence"),
        ({"confidence": "high"}, "invalid_confidence"),
        ({"evidence": "ci:run-42"}, "invalid_evidence"),
        ({"evidence": [1]}, "invalid_evidence"),
        ({"evidence": [""]}, "invalid_evidence"),
        ({"estimated_tokens": -1}, "invalid_estimated_tokens"),
        ({"case_id": 3}, "invalid_case_id"),
    ],
)
def test_proposal_submit_rejects_malformed_bodies(override: dict[str, Any], error: str) -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        response = client.post("/api/proactive/proposals", json={**_VALID_PROPOSAL, **override})
    assert response.status_code == 400
    assert response.json()["error"] == error
    assert fake.calls == []


def test_proposal_decide_forwards_an_approval() -> None:
    fake = FakeGovernance({"proactive.proposal.decide": {"proposal": {"status": "approved"}}})
    with _client(fake) as client:
        response = client.post(
            "/api/proactive/proposals/p1/decide",
            json={"decision": "approve", "by": "alice"},
        )
    assert response.status_code == 200
    assert fake.calls == [
        (
            "proactive.proposal.decide",
            {
                "proposal_id": "p1",
                "decision": "approve",
                "by": "alice",
                "principal_kind": proactive_api.DEFAULT_PRINCIPAL_KIND,
                "principal_id": proactive_api.DEFAULT_PRINCIPAL_ID,
            },
        )
    ]


def test_proposal_decide_forwards_a_rejection_with_its_reason() -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        client.post(
            "/api/proactive/proposals/p2/decide",
            json={"decision": "reject", "by": "bob", "reason": "not worth it"},
        )
    payload = fake.calls[0][1]
    assert payload["decision"] == "reject"
    assert payload["reason"] == "not worth it"


@pytest.mark.parametrize("decision", ["approve!", "APPROVE", "", 3, None])
def test_proposal_decide_rejects_an_unknown_decision(decision: object) -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        response = client.post("/api/proactive/proposals/p1/decide", json={"decision": decision})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_decision"
    assert fake.calls == []


def test_audit_forwards_limit_and_filters() -> None:
    entries = {"entries": [{"action": "admit_proposal", "allowed": True}]}
    fake = FakeGovernance({"proactive.audit.list": entries})
    with _client(fake) as client:
        response = client.get(
            "/api/proactive/audit",
            params={"limit": "25", "proposal_id": "p1", "case_id": "case-1"},
        )
    assert response.status_code == 200
    assert response.json() == entries
    assert fake.calls[0] == (
        "proactive.audit.list",
        {"limit": 25, "proposal_id": "p1", "case_id": "case-1"},
    )


# ── watchdog ─────────────────────────────────────────────────


def test_watchdog_stall_forwards_the_canonical_payload() -> None:
    verdict = {
        "verdict": "replan",
        "attempt": 1,
        "delay_ms": 1000,
        "preserve_effects": ["eff_push"],
        "replans_remaining": 1,
        "attempts_remaining": 3,
    }
    fake = FakeGovernance({"watchdog.stall.report": verdict})
    with _client(fake) as client:
        response = client.post(
            "/api/watchdog/stall",
            json={
                "task_id": "task-1",
                "subtask_id": "s1",
                "no_progress_count": 2,
                "confirmed_effect_ids": ["eff_push", "eff_push"],
                "attempts_used": 3,
                "already_retried": True,
                "ignored_extra": "dropped",
            },
        )
    assert response.status_code == 200
    assert response.json() == verdict
    op, payload = fake.calls[0]
    assert op == "watchdog.stall.report"
    assert payload == {
        "task_id": "task-1",
        "subtask_id": "s1",
        "no_progress_count": 2,
        "confirmed_effect_ids": ["eff_push"],
        "attempts_used": 3,
        "replans_used": 0,
        "already_retried": True,
    }


def test_watchdog_stall_rejects_a_malformed_report() -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        missing_task = client.post("/api/watchdog/stall", json={"no_progress_count": 2})
        bad_flag = client.post(
            "/api/watchdog/stall", json={"task_id": "task-1", "already_retried": "yes"}
        )
    assert missing_task.status_code == 400
    assert missing_task.json()["error"] == "invalid_stall_report"
    assert bad_flag.status_code == 400
    assert fake.calls == []


def test_watchdog_second_stall_escalates_instead_of_looping() -> None:
    escalation = {
        "verdict": "escalate_needs_human",
        "reason": "stalled for 2 attempts and the replan budget is exhausted",
        "replans_remaining": 0,
        "attempts_remaining": 2,
    }
    fake = FakeGovernance({"watchdog.stall.report": escalation})
    with _client(fake) as client:
        response = client.post(
            "/api/watchdog/stall",
            json={"task_id": "task-1", "no_progress_count": 2, "replans_used": 2},
        )
    assert response.status_code == 200
    assert response.json()["verdict"] == "escalate_needs_human"
    action = apply_verdict(response.json(), confirmed_effect_ids=["eff_push"])
    assert action.action == "escalate"
    assert action.preserve_effects == ()
    assert action.replans_remaining == 0


# ── availability and failures ────────────────────────────────


@pytest.mark.parametrize("method, path, body", _ENDPOINTS)
def test_missing_governance_client_returns_503(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    app = FastAPI()
    app.include_router(proactive_api.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(method.upper(), path, json=body)
    assert response.status_code == 503
    assert response.json() == {"error": "governance_unavailable"}


@pytest.mark.parametrize("method, path, body", _ENDPOINTS)
def test_governance_unavailable_returns_503(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    fake = FakeGovernance(error=GovernanceUnavailable("nats request timed out"))
    with _client(fake) as client:
        response = client.request(method.upper(), path, json=body)
    assert response.status_code == 503
    assert response.json() == {"error": "governance_unavailable"}
    assert fake.calls, "the handler must have tried the bridge before reporting it unavailable"


def test_an_unexpected_bug_is_not_masked_as_unavailable() -> None:
    fake = FakeGovernance(error=RuntimeError("a real bug"))
    with _client(fake) as client:
        response = client.get("/api/proactive/state")
    assert response.status_code == 500


def test_a_gateway_rejection_is_a_4xx_with_the_gateway_code() -> None:
    fake = FakeGovernance(
        error=GovernanceError("proactive_mode_disallows", "mode disallows evaluate")
    )
    with _client(fake) as client:
        response = client.post("/api/proactive/mode", json={"mode": "propose"})
    assert response.status_code == 400
    assert response.json() == {
        "error": "proactive_mode_disallows",
        "detail": "mode disallows evaluate",
    }


def test_the_router_uses_the_shared_transport_exception_types() -> None:
    assert proactive_api.GovernanceUnavailable is transport.GovernanceUnavailable
    assert proactive_api.GovernanceError is transport.GovernanceError


def test_a_non_object_gateway_answer_is_unavailable() -> None:
    fake = FakeGovernance({"proactive.state.get": ["not", "an", "object"]})
    with _client(fake) as client:
        response = client.get("/api/proactive/state")
    assert response.status_code == 503
    assert response.json() == {"error": "governance_unavailable"}


def test_invalid_json_body_is_a_400() -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        response = client.post(
            "/api/proactive/mode",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"
    assert fake.calls == []


def test_non_object_body_is_a_400() -> None:
    fake = FakeGovernance()
    with _client(fake) as client:
        response = client.post(
            "/api/proactive/mode",
            content="[1, 2]",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_body"
    assert fake.calls == []


# ── auth gate seam ──────────────────────────────────────────


def test_the_host_auth_gate_runs_before_the_bridge() -> None:
    def deny(request: Any) -> JSONResponse:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    fake = FakeGovernance()
    with _client(fake, auth=deny) as client:
        response = client.get("/api/proactive/state")
    assert response.status_code == 401
    assert fake.calls == [], "an unauthenticated request must not reach the governance bridge"


def test_an_allowing_auth_gate_is_transparent() -> None:
    def allow(request: Any) -> None:
        return None

    fake = FakeGovernance({"proactive.state.get": {"state": {"mode": "off"}}})
    with _client(fake, auth=allow) as client:
        response = client.get("/api/proactive/state")
    assert response.status_code == 200
    assert fake.calls == [("proactive.state.get", {})]


def test_the_host_gate_advertised_on_app_state_gates_the_module_router() -> None:
    """``governance_routes`` mounts the module-level router; the host gate still applies."""

    def deny(request: Any) -> JSONResponse:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    fake = FakeGovernance()
    app = FastAPI()
    app.state.governance_client = fake
    setattr(app.state, proactive_api.AUTH_CHECK_STATE_ATTR, deny)
    app.include_router(proactive_api.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/proactive/mode", json={"mode": "observe"})
    assert response.status_code == 401
    assert fake.calls == [], "the gate must run before the governance bridge"


def test_the_module_router_is_open_when_no_gate_is_advertised() -> None:
    fake = FakeGovernance({"proactive.state.get": {"state": {"mode": "off"}}})
    app = FastAPI()
    app.state.governance_client = fake
    app.include_router(proactive_api.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/proactive/state")
    assert response.status_code == 200
    assert fake.calls == [("proactive.state.get", {})]


def test_the_module_router_exposes_every_documented_route() -> None:
    routes = {
        (method, route.path)
        for route in proactive_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == {
        ("GET", "/api/proactive/state"),
        ("POST", "/api/proactive/mode"),
        ("GET", "/api/proactive/proposals"),
        ("POST", "/api/proactive/proposals"),
        ("POST", "/api/proactive/proposals/{proposal_id}/decide"),
        ("GET", "/api/proactive/audit"),
        ("POST", "/api/watchdog/stall"),
    }
