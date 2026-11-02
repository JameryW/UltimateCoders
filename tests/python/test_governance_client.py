"""Tests for the Python→gateway governance bridge client.

The client is the only way the dashboard reaches governance state, so its failure
modes must be distinguishable: a transport failure is an outage (503), while a
rejection is a request problem (4xx). These tests pin that split without a live
NATS server by using a stub client that mimics nats-py's ``request`` coroutine.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from ultimate_coders.governance_client import (
    DEFAULT_TIMEOUT_SECONDS,
    GOVERNANCE_SUBJECT,
    GovernanceClient,
    GovernanceError,
    GovernanceUnavailable,
    configure_default_client,
    get_default_governance_client,
    reset_default_client,
    summarize_capacity,
)


class _Reply:
    def __init__(self, payload: bytes) -> None:
        self.data = payload


class _StubNats:
    """Minimal stand-in for a nats-py client's request path."""

    def __init__(self, reply: Any = None, *, delay: float = 0.0, raises: Exception | None = None):
        self.reply = reply
        self.delay = delay
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def request(self, subject: str, payload: bytes) -> _Reply:
        self.calls.append({"subject": subject, "payload": json.loads(payload.decode("utf-8"))})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        body = self.reply
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        return _Reply(body)


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _err(code: str, message: str = "nope") -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def test_call_without_connection_is_unavailable_not_an_error() -> None:
    client = GovernanceClient(None)
    assert client.is_connected is False
    with pytest.raises(GovernanceUnavailable):
        asyncio.run(client.call("capacity.get", {}))


def test_call_sends_op_and_payload_on_the_documented_subject() -> None:
    nats = _StubNats(_ok({"capacity": {"worker_slots_total": 2}}))
    client = GovernanceClient(nats)
    result = asyncio.run(client.call("capacity.get", {"limit": 5}))
    assert result == {"capacity": {"worker_slots_total": 2}}
    assert nats.calls[0]["subject"] == GOVERNANCE_SUBJECT == "uc.governance.request"
    assert nats.calls[0]["payload"] == {"op": "capacity.get", "payload": {"limit": 5}}


def test_rejection_becomes_governance_error_with_the_code() -> None:
    nats = _StubNats(_err("proactive_mode_disallows", "mode is off"))
    client = GovernanceClient(nats)
    with pytest.raises(GovernanceError) as caught:
        asyncio.run(client.call("proactive.proposal.submit", {}))
    assert caught.value.code == "proactive_mode_disallows"
    assert "mode is off" in caught.value.message


def test_transport_exception_becomes_unavailable() -> None:
    nats = _StubNats(raises=ConnectionError("nats closed"))
    client = GovernanceClient(nats)
    with pytest.raises(GovernanceUnavailable):
        asyncio.run(client.call("capacity.get", {}))


def test_timeout_becomes_unavailable() -> None:
    nats = _StubNats(_ok({}), delay=DEFAULT_TIMEOUT_SECONDS + 1)
    client = GovernanceClient(nats, timeout=0.05)
    with pytest.raises(GovernanceUnavailable):
        asyncio.run(client.call("capacity.get", {}))


def test_non_json_reply_is_unavailable_rather_than_a_crash() -> None:
    nats = _StubNats(b"<html>gateway down</html>")
    client = GovernanceClient(nats)
    with pytest.raises(GovernanceUnavailable):
        asyncio.run(client.call("capacity.get", {}))


def test_ok_reply_without_result_yields_an_empty_mapping() -> None:
    nats = _StubNats({"ok": True})
    client = GovernanceClient(nats)
    assert asyncio.run(client.call("capacity.get", {})) == {}


def test_typed_helpers_shape_the_payloads() -> None:
    nats = _StubNats(_ok({}))
    client = GovernanceClient(nats)
    asyncio.run(
        client.submit_task("fix the build", "proj", submission_id="sub-1", case_id="case-1")
    )
    assert nats.calls[-1]["payload"] == {
        "op": "submission.submit",
        "payload": {
            "description": "fix the build",
            "project_id": "proj",
            "principal_kind": "service",
            "principal_id": "dashboard",
            "submission_id": "sub-1",
            "case_id": "case-1",
        },
    }

    asyncio.run(client.list_cases(status="needs_human", limit=7))
    assert nats.calls[-1]["payload"]["payload"] == {
        "limit": 7,
        "include_terminal": False,
        "status": "needs_human",
    }

    asyncio.run(client.resolve_attention("att-1", "human_decision", by="alice"))
    assert nats.calls[-1]["payload"]["payload"]["resolution_kind"] == "human_decision"
    assert nats.calls[-1]["payload"]["payload"]["by"] == "alice"


def test_attach_replaces_a_missing_connection() -> None:
    client = GovernanceClient(None)
    nats = _StubNats(_ok({"state": {"mode": "off"}}))
    client.attach(nats)
    assert client.is_connected is True
    assert asyncio.run(client.get_proactive_state()) == {"state": {"mode": "off"}}


def test_default_client_registry_round_trip() -> None:
    reset_default_client()
    assert get_default_governance_client() is None
    client = GovernanceClient(None)
    assert configure_default_client(client) is client
    assert get_default_governance_client() is client
    reset_default_client()
    assert get_default_governance_client() is None


def test_capacity_summary_never_reports_missing_as_authoritative_zero() -> None:
    summary = summarize_capacity(
        {
            "worker_slots_total": 8,
            "worker_slots_busy": 3,
            "waiting_event_cases": 2,
            "slots_saved_by_quiet_waiting": 2,
        }
    )
    assert summary["worker_slots_total"] == 8
    assert summary["worker_slots_busy"] == 3
    # Absent keys default to 0 for rendering, and the raw mapping is what tells a
    # caller whether the gateway answered at all.
    assert summary["needs_human_items"] == 0
    assert set(summary) == {
        "worker_slots_total",
        "worker_slots_busy",
        "worker_slots_reserved_human",
        "waiting_event_cases",
        "verifying_cases",
        "needs_human_items",
        "blocked_cases",
        "worker_slots_available",
        "slots_saved_by_quiet_waiting",
    }
