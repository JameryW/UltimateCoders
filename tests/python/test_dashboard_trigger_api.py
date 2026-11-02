"""PR04 -- the dashboard's trigger door.

The gateway owns ingest: dedup, subscription matching and case routing. These
tests inject a fake governance client that implements the same observable
contract (dedup by the Rust rule, thread -> case routing) so the API's
forwarding and refusal behaviour is proven without Rust. Acceptance covered
here: three different events on one pull request route to one case; an
identical delivery is reported duplicate; a bad HMAC is rejected before the
gateway is called; an unreachable gateway is 503, never 500.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ultimate_coders.dashboard import trigger_api

SECRET = "webhook-secret"
TOKEN = "dashboard-token"
REPO = "acme/widgets"
PR_NUMBER = 12
COMMENT_ID = 5555
CHECK_RUN_ID = 424242
HEAD_SHA = "a" * 40


# ── Fake gateway (same observable contract as the NATS bridge) ─────────


def _dedup_key(payload: dict[str, Any]) -> str:
    """The gateway's rule, re-implemented locally: no Rust call in tests."""
    body = json.loads(payload["payload_json"])
    payload_digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    material = f"{payload['source']}|{payload['kind']}|{payload['external_id']}|{payload_digest}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class FakeGovernance:
    """Minimal stand-in for the NATS request/reply bridge."""

    def __init__(self, subscriptions: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.subscriptions = [dict(subscription) for subscription in (subscriptions or [])]
        self.events: dict[str, dict[str, Any]] = {}
        self.thread_cases: dict[str, str] = {}
        self.opened: list[str] = []

    async def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, payload))
        if op == "trigger.ingest":
            return self._ingest(payload)
        if op == "trigger.subscription.list":
            case_id = payload.get("case_id")
            return {
                "subscriptions": [
                    subscription
                    for subscription in self.subscriptions
                    if case_id is None or subscription["case_id"] == case_id
                ]
            }
        raise AssertionError(f"unexpected governance op {op!r}")

    def _ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = _dedup_key(payload)
        if key in self.events:
            return {
                **self.events[key],
                "accepted": False,
                "duplicate": True,
                "event_id": "",
                "matched_cases": [],
            }
        matched = self._matching_cases(payload)
        outcome: dict[str, Any] = {
            "accepted": True,
            "duplicate": False,
            "event_id": f"evt-{len(self.events) + 1}",
            "dedup_key": key,
            "matched_cases": matched,
        }
        if not matched and payload.get("open_case_if_unmatched"):
            case_id = f"case-{len(self.opened) + 1}"
            self.opened.append(case_id)
            self.thread_cases[payload.get("thread_key") or payload["external_id"]] = case_id
            outcome["matched_cases"] = [case_id]
            outcome["opened_case_id"] = case_id
        elif not matched:
            outcome["unmatched_reason"] = "thread_has_no_case"
        self.events[key] = outcome
        return outcome

    def _matching_cases(self, payload: dict[str, Any]) -> list[str]:
        matched: list[str] = []
        thread_key = payload.get("thread_key")
        if thread_key and thread_key in self.thread_cases:
            matched.append(self.thread_cases[thread_key])
        for subscription in self.subscriptions:
            if not subscription.get("active", True):
                continue
            if subscription["source"] != payload["source"]:
                continue
            if subscription.get("repo") and subscription["repo"] != payload.get("repo"):
                continue
            if subscription.get("thread_key") and subscription["thread_key"] != payload.get(
                "thread_key"
            ):
                continue
            kinds = subscription.get("kinds") or []
            if kinds and payload["kind"] not in kinds:
                continue
            if subscription["case_id"] not in matched:
                matched.append(subscription["case_id"])
        return matched


class UnavailableGovernance:
    async def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise trigger_api.GovernanceUnavailable("bridge down")


class BrokenTransportGovernance:
    """A client that lets the raw transport error escape."""

    async def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("nats connection lost")


class BadReplyGovernance:
    async def call(self, op: str, payload: dict[str, Any]) -> str:
        return "not a dict"


def _subscription(
    case_id: str = "case-1",
    *,
    source: str = "github",
    thread_key: str = f"{REPO}#{PR_NUMBER}",
) -> dict[str, Any]:
    return {
        "id": f"sub-{case_id}",
        "case_id": case_id,
        "source": source,
        "repo": REPO,
        "thread_key": thread_key,
        "kinds": [],
        "labels": [],
        "active": True,
        "created_at": 0,
        "event_count": 0,
    }


def _make_client(governance: Any) -> TestClient:
    application = FastAPI()
    application.include_router(trigger_api.router)
    if governance is not None:
        application.state.governance_client = governance
    return TestClient(application)


# ── Request helpers ────────────────────────────────────────────────────


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _signed_headers(
    event: str,
    body: bytes,
    delivery: str = "delivery-1",
    secret: str | None = SECRET,
) -> dict[str, str]:
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    if secret is not None:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return headers


def _issue_comment_payload() -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {
            "id": 9001,
            "number": PR_NUMBER,
            "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"},
        },
        "comment": {"id": COMMENT_ID, "body": "please take a look"},
        "repository": {"full_name": REPO},
        "sender": {"login": "octocat"},
    }


def _check_run_payload() -> dict[str, Any]:
    return {
        "action": "completed",
        "check_run": {
            "id": CHECK_RUN_ID,
            "status": "completed",
            "conclusion": "success",
            "head_sha": HEAD_SHA,
            "pull_requests": [{"number": PR_NUMBER}],
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "octocat"},
    }


def _pull_request_payload() -> dict[str, Any]:
    return {
        "action": "synchronize",
        "number": PR_NUMBER,
        "pull_request": {
            "id": 7777,
            "number": PR_NUMBER,
            "head": {"sha": HEAD_SHA},
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "octocat"},
    }


def _post_webhook(
    client: TestClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str = "delivery-1",
    secret: str | None = SECRET,
):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/api/triggers/github",
        content=body,
        headers=_signed_headers(event, body, delivery=delivery, secret=secret),
    )


@pytest.fixture(autouse=True)
def _secret_and_token(monkeypatch):
    monkeypatch.setenv("UC_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("UC_DASHBOARD_TOKEN", raising=False)


# ── Webhook path ───────────────────────────────────────────────────────


def test_signed_webhook_is_forwarded_and_accepted() -> None:
    governance = FakeGovernance(subscriptions=[_subscription()])
    client = _make_client(governance)

    response = _post_webhook(client, "issue_comment", _issue_comment_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["duplicate"] is False
    assert body["event_id"] == "evt-1"
    assert body["matched_cases"] == ["case-1"]
    assert body["dedup_key"] == _dedup_key(governance.calls[0][1])

    op, forwarded = governance.calls[0]
    assert op == "trigger.ingest"
    assert forwarded["source"] == "github"
    assert forwarded["kind"] == "pull_request_comment"
    assert forwarded["external_id"] == f"comment:{COMMENT_ID}"
    assert forwarded["thread_key"] == f"{REPO}#{PR_NUMBER}"
    assert forwarded["repo"] == REPO
    assert forwarded["actor"] == "octocat"
    assert forwarded["delivery_id"] == "delivery-1"
    assert forwarded["principal_kind"] == "service"
    assert json.loads(forwarded["payload_json"]) == {
        "action": "created",
        "number": PR_NUMBER,
        "comment_id": COMMENT_ID,
    }


def test_bad_signature_is_rejected_before_any_gateway_call() -> None:
    governance = FakeGovernance(subscriptions=[_subscription()])
    client = _make_client(governance)

    response = _post_webhook(
        client, "issue_comment", _issue_comment_payload(), secret="not-the-secret"
    )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_signature"
    assert governance.calls == []


def test_missing_signature_is_rejected() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    response = _post_webhook(client, "issue_comment", _issue_comment_payload(), secret=None)
    assert response.status_code == 401
    assert governance.calls == []


def test_unconfigured_secret_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("UC_GITHUB_WEBHOOK_SECRET")
    governance = FakeGovernance()
    client = _make_client(governance)

    response = _post_webhook(client, "issue_comment", _issue_comment_payload())

    assert response.status_code == 503
    assert response.json()["error"] == "webhook_secret_not_configured"
    assert governance.calls == []


def test_redelivery_is_reported_as_duplicate() -> None:
    governance = FakeGovernance(subscriptions=[_subscription()])
    client = _make_client(governance)
    payload = _issue_comment_payload()

    first = _post_webhook(client, "issue_comment", payload, delivery="delivery-1")
    second = _post_webhook(client, "issue_comment", payload, delivery="delivery-2")

    assert first.status_code == 202 and first.json()["accepted"] is True
    assert second.status_code == 202
    assert second.json()["accepted"] is False
    assert second.json()["duplicate"] is True
    # Both deliveries reach the gateway; the gateway (not the door) dedupes.
    assert len(governance.events) == 1
    assert len(governance.calls) == 2


def test_comment_ci_result_and_status_change_route_to_one_case() -> None:
    """Acceptance: one PR thread, one case -- no duplicate case is opened."""
    governance = FakeGovernance(subscriptions=[_subscription()])
    client = _make_client(governance)

    responses = [
        _post_webhook(client, "issue_comment", _issue_comment_payload(), delivery="d-1"),
        _post_webhook(client, "check_run", _check_run_payload(), delivery="d-2"),
        _post_webhook(client, "pull_request", _pull_request_payload(), delivery="d-3"),
    ]

    assert [response.status_code for response in responses] == [202, 202, 202]
    assert [response.json()["matched_cases"] for response in responses] == [
        ["case-1"],
        ["case-1"],
        ["case-1"],
    ]
    assert {payload["thread_key"] for _op, payload in governance.calls} == {
        f"{REPO}#{PR_NUMBER}"
    }
    assert {payload["kind"] for _op, payload in governance.calls} == {
        "pull_request_comment",
        "check_run",
        "pull_request_synchronize",
    }
    assert len(governance.events) == 3
    assert governance.opened == []


def test_ignored_event_is_acknowledged_without_a_gateway_call() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)

    response = _post_webhook(client, "star", {"action": "created"})

    assert response.status_code == 202
    assert response.json()["ignored"] is True
    assert governance.calls == []


def test_invalid_json_is_rejected() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    body = b"not json"
    response = client.post(
        "/api/triggers/github", content=body, headers=_signed_headers("push", body)
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_payload"
    assert governance.calls == []


def test_missing_event_header_is_rejected() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    body = json.dumps(_check_run_payload()).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _sign(body),
    }
    response = client.post("/api/triggers/github", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_payload"
    assert governance.calls == []


def test_oversized_webhook_is_rejected() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    body = b"{" + b" " * trigger_api.MAX_PAYLOAD_BYTES
    response = client.post(
        "/api/triggers/github", content=body, headers=_signed_headers("push", body)
    )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"
    assert governance.calls == []


# ── Generic ingest path ────────────────────────────────────────────────


def test_generic_ingest_forwards_the_canonical_payload() -> None:
    governance = FakeGovernance(subscriptions=[_subscription(source="ci")])
    client = _make_client(governance)
    payload = {"status": "completed", "conclusion": "success", "run_id": "ci-9"}

    response = client.post(
        "/api/triggers/ingest",
        json={
            "source": "ci",
            "kind": "check_run",
            "external_id": "ci-run-9",
            "thread_key": f"{REPO}#{PR_NUMBER}",
            "repo": REPO,
            "actor": "ci-bot",
            "payload": payload,
        },
    )

    assert response.status_code == 202
    assert response.json()["matched_cases"] == ["case-1"]
    op, forwarded = governance.calls[0]
    assert op == "trigger.ingest"
    assert forwarded["source"] == "ci"
    assert forwarded["kind"] == "check_run"
    assert forwarded["external_id"] == "ci-run-9"
    assert json.loads(forwarded["payload_json"]) == payload
    assert forwarded["principal_kind"] == "service"
    assert forwarded["open_case_if_unmatched"] is False


def test_generic_ingest_requires_token_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("UC_DASHBOARD_TOKEN", TOKEN)
    governance = FakeGovernance()
    client = _make_client(governance)
    body = {"source": "api", "kind": "push", "external_id": "api-1", "payload": {}}

    assert client.post("/api/triggers/ingest", json=body).status_code == 401
    assert (
        client.post(
            "/api/triggers/ingest", json=body, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/triggers/ingest",
            json=body,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).status_code
        == 202
    )
    assert len(governance.calls) == 1


def test_generic_ingest_without_token_config_is_open() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    response = client.post(
        "/api/triggers/ingest",
        json={"source": "manual", "kind": "issue_opened", "external_id": "m-1", "payload": {}},
    )
    assert response.status_code == 202
    assert len(governance.calls) == 1


@pytest.mark.parametrize(
    "body, expected_error",
    [
        ({"source": "nope", "kind": "check_run", "external_id": "x"}, "unknown_source"),
        ({"source": "ci", "kind": "nope", "external_id": "x"}, "unknown_kind"),
        ({"source": "ci", "kind": "check_run", "external_id": ""}, "invalid_external_id"),
        (
            {"source": "ci", "kind": "check_run", "external_id": "x", "payload": []},
            "invalid_payload",
        ),
        (
            {
                "source": "ci",
                "kind": "check_run",
                "external_id": "x",
                "open_case_if_unmatched": "yes",
            },
            "invalid_request",
        ),
    ],
)
def test_generic_ingest_rejects_malformed_requests(
    body: dict[str, Any], expected_error: str
) -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    response = client.post("/api/triggers/ingest", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == expected_error
    assert governance.calls == []


def test_generic_ingest_rejects_a_non_object_body() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    response = client.post("/api/triggers/ingest", json=[1, 2, 3])
    assert response.status_code == 400
    assert governance.calls == []


def test_generic_ingest_can_open_a_case_when_asked() -> None:
    """The explicit entry point: an unmatched thread may open a case."""
    governance = FakeGovernance()
    client = _make_client(governance)
    response = client.post(
        "/api/triggers/ingest",
        json={
            "source": "api",
            "kind": "pull_request_opened",
            "external_id": "pr-99",
            "thread_key": f"{REPO}#99",
            "open_case_if_unmatched": True,
            "open_case_project_id": "project-1",
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["matched_cases"] == ["case-1"]
    assert body["opened_case_id"] == "case-1"
    assert governance.opened == ["case-1"]
    assert governance.calls[0][1]["open_case_project_id"] == "project-1"


def test_webhook_never_opens_a_case_on_its_own() -> None:
    """Unmatched GitHub deliveries are reported, not turned into cases."""
    governance = FakeGovernance()
    client = _make_client(governance)
    response = _post_webhook(client, "issue_comment", _issue_comment_payload())
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["matched_cases"] == []
    assert body["unmatched_reason"] == "thread_has_no_case"
    assert governance.opened == []
    assert governance.calls[0][1]["open_case_if_unmatched"] is False


def test_a_thread_case_is_not_opened_again_by_its_webhook() -> None:
    """Acceptance: the case opened for a thread keeps receiving its events."""
    governance = FakeGovernance()
    client = _make_client(governance)

    opened = client.post(
        "/api/triggers/ingest",
        json={
            "source": "api",
            "kind": "pull_request_opened",
            "external_id": "pr-12",
            "thread_key": f"{REPO}#{PR_NUMBER}",
            "open_case_if_unmatched": True,
        },
    )
    assert opened.json()["opened_case_id"] == "case-1"

    comment = _post_webhook(client, "issue_comment", _issue_comment_payload(), delivery="d-2")
    ci = _post_webhook(client, "check_run", _check_run_payload(), delivery="d-3")

    assert comment.json()["matched_cases"] == ["case-1"]
    assert ci.json()["matched_cases"] == ["case-1"]
    assert comment.json()["opened_case_id"] is None
    assert governance.opened == ["case-1"]


# ── Read path ──────────────────────────────────────────────────────────


def test_subscriptions_are_read_from_the_gateway() -> None:
    governance = FakeGovernance(subscriptions=[_subscription()])
    client = _make_client(governance)

    response = client.get("/api/triggers/subscriptions", params={"case_id": "case-1"})

    assert response.status_code == 200
    assert [subscription["case_id"] for subscription in response.json()["subscriptions"]] == [
        "case-1"
    ]
    op, forwarded = governance.calls[0]
    assert op == "trigger.subscription.list"
    assert forwarded == {"case_id": "case-1"}


def test_subscriptions_forward_an_explicit_limit() -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    assert client.get("/api/triggers/subscriptions", params={"limit": "5"}).status_code == 200
    assert governance.calls[0][1] == {"limit": 5}


@pytest.mark.parametrize("limit", ["abc", "0", "-1", "1001"])
def test_subscriptions_reject_an_invalid_limit(limit: str) -> None:
    governance = FakeGovernance()
    client = _make_client(governance)
    response = client.get("/api/triggers/subscriptions", params={"limit": limit})
    assert response.status_code == 400
    assert governance.calls == []


# ── Gateway unavailability ─────────────────────────────────────────────


def test_governance_unavailable_is_503_not_500() -> None:
    client = _make_client(UnavailableGovernance())

    webhook = _post_webhook(client, "check_run", _check_run_payload())
    assert webhook.status_code == 503
    assert webhook.json() == {"error": "governance_unavailable"}

    listing = client.get("/api/triggers/subscriptions")
    assert listing.status_code == 503
    assert listing.json() == {"error": "governance_unavailable"}


def test_raw_transport_failure_is_503_not_500() -> None:
    """A client that leaks the transport error still means "unavailable"."""
    client = _make_client(BrokenTransportGovernance())
    response = _post_webhook(client, "check_run", _check_run_payload())
    assert response.status_code == 503
    assert response.json() == {"error": "governance_unavailable"}


def test_missing_governance_client_is_503_not_500() -> None:
    client = _make_client(None)

    assert _post_webhook(client, "check_run", _check_run_payload()).status_code == 503
    assert client.get("/api/triggers/subscriptions").status_code == 503
    assert (
        client.post(
            "/api/triggers/ingest",
            json={"source": "ci", "kind": "check_run", "external_id": "x"},
        ).status_code
        == 503
    )


def test_non_object_governance_reply_is_a_contract_error() -> None:
    client = _make_client(BadReplyGovernance())
    response = _post_webhook(client, "check_run", _check_run_payload())
    assert response.status_code == 502
    assert response.json()["error"] == "governance_bad_reply"
