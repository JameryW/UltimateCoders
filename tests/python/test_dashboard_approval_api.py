"""The dashboard approval router must project the queue and forward the decision.

The gateway is faked through ``app.state.governance_client``: the contract under
test is the op name + payload on the way out and the JSON shape on the way back.

Pinned here:

* there is **no** create endpoint (the UI cannot claim to be the requester);
* the decision is forwarded as a ``user`` principal, never as a service or an
  agent — that would defeat the approval;
* a configured ``UC_DASHBOARD_TOKEN`` gates the decision, and an unconfigured one
  leaves it open (the same rule the gateway applies);
* a dead bridge is ``503 {"error": "governance_unavailable"}``, never a 500.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ultimate_coders.dashboard import approval_api
from ultimate_coders.dashboard.approval_api import (
    OP_APPROVAL_DECIDE,
    OP_APPROVAL_LIST,
    TOKEN_ENV,
    router,
)

_UNSET = object()

PENDING = {
    "id": "apr-abc123",
    "tool": "scale_workers",
    "case_id": "case-1",
    "project_id": "proj-1",
    "action_digest": "scale:worker=3",
    "requested_by_kind": "agent",
    "requested_by_id": "executor",
    "reason": "queue is backing up",
    "state": "pending",
    "requested_at": "2026-01-01T00:00:00Z",
    "expires_at": "2026-01-01T00:15:00Z",
}

APPROVED = dict(
    PENDING,
    state="approved",
    decided_at="2026-01-01T00:05:00Z",
    decided_by_id="alice",
    note="ok",
)


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
    app = FastAPI()
    app.include_router(router)
    if client is not _UNSET:
        app.state.governance_client = client
    return app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setenv("UC_OPERATOR_ID", "alice")


def test_listing_returns_the_queue_unchanged() -> None:
    client = FakeGovernanceClient({OP_APPROVAL_LIST: {"approvals": [PENDING]}})
    response = TestClient(_app(client)).get("/api/approvals")
    assert response.status_code == 200
    assert response.json() == {"approvals": [PENDING]}
    assert client.calls == [(OP_APPROVAL_LIST, {"limit": 50})]


def test_listing_can_filter_by_state_and_rejects_an_unknown_one() -> None:
    client = FakeGovernanceClient({OP_APPROVAL_LIST: {"approvals": []}})
    app = TestClient(_app(client))
    assert app.get("/api/approvals", params={"state": "Pending"}).status_code == 200
    assert client.calls[-1] == (OP_APPROVAL_LIST, {"limit": 50, "state": "pending"})

    assert app.get("/api/approvals", params={"state": "maybe"}).status_code == 400


def test_there_is_no_create_endpoint() -> None:
    """The dashboard must not pretend to be the principal that wants the action."""
    client = FakeGovernanceClient({OP_APPROVAL_LIST: {"approvals": []}})
    client_app = TestClient(_app(client))
    assert client_app.post("/api/approvals", json={"tool": "scale_workers"}).status_code == 405


def test_deciding_forwards_a_user_principal() -> None:
    client = FakeGovernanceClient({OP_APPROVAL_DECIDE: {"approval": APPROVED}})
    response = TestClient(_app(client)).post(
        "/api/approvals/apr-abc123/decide", json={"approve": True, "note": "ok"}
    )
    assert response.status_code == 200
    assert response.json() == {"approval": APPROVED}
    op, payload = client.calls[0]
    assert op == OP_APPROVAL_DECIDE
    assert payload == {
        "approval_id": "apr-abc123",
        "approve": True,
        "note": "ok",
        "principal_kind": "user",
        "principal_id": "alice",
    }


def test_deciding_requires_the_token_when_one_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    client = FakeGovernanceClient({OP_APPROVAL_DECIDE: {"approval": APPROVED}})
    app = TestClient(_app(client))

    assert app.post("/api/approvals/apr-abc123/decide", json={"approve": True}).status_code == 401
    assert client.calls == []

    ok = app.post(
        "/api/approvals/apr-abc123/decide",
        json={"approve": False, "note": "not now"},
        headers={"authorization": "Bearer s3cret"},
    )
    assert ok.status_code == 200
    assert client.calls[0][1]["approve"] is False


def test_a_malformed_decision_body_is_a_400() -> None:
    client = FakeGovernanceClient({OP_APPROVAL_DECIDE: {"approval": APPROVED}})
    app = TestClient(_app(client))
    assert app.post("/api/approvals/apr-abc123/decide", json={"approve": "yes"}).status_code == 400
    assert app.post("/api/approvals/apr-abc123/decide", content=b"not json").status_code == 400
    assert client.calls == []


def test_a_dead_bridge_is_503_not_500() -> None:
    unavailable = TestClient(_app())
    assert unavailable.get("/api/approvals").status_code == 503
    assert unavailable.post(
        "/api/approvals/apr-abc123/decide", json={"approve": True}
    ).status_code == 503

    broken = TestClient(_app(FakeGovernanceClient({}, error=RuntimeError("bridge down"))))
    response = broken.get("/api/approvals")
    assert response.status_code == 503
    assert response.json()["error"] == "governance_unavailable"

# ── CSV export ───────────────────────────────────────────────


def _approval(index: int, **overrides: Any) -> dict[str, Any]:
    return dict(PENDING, id=f"apr-{index:04d}", **overrides)


def _full_page(start: int, size: int) -> list[dict[str, Any]]:
    return [_approval(start + index) for index in range(size)]


def _csv_rows(response: Any) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


def test_export_walks_the_gateway_pages_until_a_short_one() -> None:
    pages = {
        0: _full_page(0, approval_api.EXPORT_PAGE_SIZE),
        approval_api.EXPORT_PAGE_SIZE: _full_page(
            approval_api.EXPORT_PAGE_SIZE, 5
        ),
    }
    client = FakeGovernanceClient(
        {OP_APPROVAL_LIST: lambda payload: {"approvals": pages[payload["offset"]]}}
    )
    response = TestClient(_app(client)).get("/api/approvals/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="approvals.csv"' == response.headers["content-disposition"]
    rows = _csv_rows(response)
    assert rows[0] == list(approval_api.EXPORT_COLUMNS)
    assert len(rows) == 1 + approval_api.EXPORT_PAGE_SIZE + 5
    assert response.headers["x-uc-approvals-returned"] == str(
        approval_api.EXPORT_PAGE_SIZE + 5
    )
    assert "x-uc-approvals-truncated" not in response.headers
    assert [payload["offset"] for _, payload in client.calls] == [
        0,
        approval_api.EXPORT_PAGE_SIZE,
    ]
    assert all(payload["limit"] == approval_api.EXPORT_PAGE_SIZE for _, payload in client.calls)


def test_export_says_so_when_it_hits_its_bound(monkeypatch: Any) -> None:
    monkeypatch.setattr(approval_api, "EXPORT_MAX_PAGES", 2)
    client = FakeGovernanceClient(
        {OP_APPROVAL_LIST: lambda payload: {"approvals": _full_page(payload["offset"], 200)}}
    )
    response = TestClient(_app(client)).get("/api/approvals/export.csv")

    assert response.status_code == 200
    assert response.headers["x-uc-approvals-truncated"] == "true"
    assert response.headers["x-uc-approvals-returned"] == "400"
    # Bounded: two calls, not an unbounded walk.
    assert len(client.calls) == 2


def test_export_filters_by_state_and_rejects_an_unknown_one() -> None:
    client = FakeGovernanceClient({OP_APPROVAL_LIST: {"approvals": [PENDING]}})
    app = TestClient(_app(client))
    assert app.get("/api/approvals/export.csv", params={"state": "Used"}).status_code == 200
    assert client.calls[0][1]["state"] == "used"
    assert app.get("/api/approvals/export.csv", params={"state": "nope"}).status_code == 400


def test_export_is_503_when_the_bridge_is_down() -> None:
    assert TestClient(_app()).get("/api/approvals/export.csv").status_code == 503
    broken = TestClient(_app(FakeGovernanceClient({}, error=RuntimeError("bridge down"))))
    assert broken.get("/api/approvals/export.csv").status_code == 503


def test_export_neutralises_spreadsheet_formulas() -> None:
    client = FakeGovernanceClient(
        {
            OP_APPROVAL_LIST: {
                "approvals": [
                    _approval(
                        0,
                        reason='=HYPERLINK("http://evil","click")',
                        note="+1 555 0100",
                        action_digest="-2+3",
                    )
                ]
            }
        }
    )
    rows = _csv_rows(TestClient(_app(client)).get("/api/approvals/export.csv"))
    header, row = rows[0], rows[1]
    cells = dict(zip(header, row, strict=True))
    for column in ("reason", "note", "action_digest"):
        assert cells[column].startswith("'"), column
        assert cells[column][1:] in {
            '=HYPERLINK("http://evil","click")',
            "+1 555 0100",
            "-2+3",
        }


def test_export_keeps_raw_wire_timestamps() -> None:
    client = FakeGovernanceClient({OP_APPROVAL_LIST: {"approvals": [PENDING]}})
    rows = _csv_rows(TestClient(_app(client)).get("/api/approvals/export.csv"))
    cells = dict(zip(rows[0], rows[1], strict=True))
    assert cells["requested_at"] == "2026-01-01T00:00:00Z"
    assert cells["expires_at"] == "2026-01-01T00:15:00Z"
    assert cells["decided_at"] == ""
