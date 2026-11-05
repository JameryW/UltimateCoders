"""Tests for the PR01 ``applied`` confirmation the orchestrator sends back.

The governance plane cannot see the worker's local dedup store, so the consumer
is the only one that can say "the orchestration exists". These tests pin the
wire shape and the two deliberate behaviours: the principal defaults to
``agent:uc-worker`` (overridable), and a failed confirmation is logged rather
than raised because the task already exists.
"""

from __future__ import annotations

import json

from ultimate_coders.governance_client import GovernanceClient
from ultimate_coders.nats_worker import NatsWorker

APPLIED_REPLY = {
    "ok": True,
    "result": {"available": True, "submission": {"state": "applied"}},
}


class _Response:
    def __init__(self, body: dict) -> None:
        self.data = json.dumps(body).encode("utf-8")


class _FakeNats:
    def __init__(self, *, body: dict | None = None, fail: Exception | None = None) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._body = body or APPLIED_REPLY
        self._fail = fail

    async def request(self, subject, data, timeout=None):  # noqa: ANN001, ANN201
        self.requests.append((subject, json.loads(data)))
        if self._fail is not None:
            raise self._fail
        return _Response(self._body)


def _worker(nc) -> NatsWorker:  # noqa: ANN001
    worker = NatsWorker.__new__(NatsWorker)
    worker._nc = nc
    return worker


async def test_the_consumer_confirms_applied_through_the_bridge() -> None:
    nc = _FakeNats()
    worker = _worker(nc)

    await worker._confirm_submission_applied("sub-1", "sub-abc")

    assert len(nc.requests) == 1
    subject, envelope = nc.requests[0]
    assert subject == "uc.governance.request"
    assert envelope["op"] == "submission.apply"
    assert envelope["payload"] == {
        "submission_id": "sub-1",
        "task_id": "sub-abc",
        "principal_kind": "agent",
        "principal_id": "uc-worker",
    }


async def test_the_consumer_identity_is_overridable(monkeypatch) -> None:
    monkeypatch.setenv("UC_WORKER_PRINCIPAL_KIND", "service")
    monkeypatch.setenv("UC_WORKER_PRINCIPAL_ID", "orchestrator")
    nc = _FakeNats()
    worker = _worker(nc)

    await worker._confirm_submission_applied("sub-1", "sub-abc")

    payload = nc.requests[0][1]["payload"]
    assert payload["principal_kind"] == "service"
    assert payload["principal_id"] == "orchestrator"


async def test_without_a_bridge_connection_the_confirmation_is_a_noop() -> None:
    worker = _worker(None)

    await worker._confirm_submission_applied("sub-1", "sub-abc")


async def test_empty_identifiers_are_not_sent() -> None:
    nc = _FakeNats()
    worker = _worker(nc)

    await worker._confirm_submission_applied("", "sub-abc")
    await worker._confirm_submission_applied("sub-1", "")

    assert nc.requests == []


async def test_a_failed_confirmation_is_logged_not_raised(caplog) -> None:  # noqa: ANN001
    nc = _FakeNats(fail=RuntimeError("nats down"))
    worker = _worker(nc)

    with caplog.at_level("WARNING"):
        await worker._confirm_submission_applied("sub-1", "sub-abc")

    assert any(
        "Could not confirm submission" in record.message for record in caplog.records
    )


async def test_the_client_omits_the_principal_unless_asked() -> None:
    nc = _FakeNats()
    client = GovernanceClient(nc)

    await client.apply_submission("sub-1", "sub-abc")

    payload = nc.requests[0][1]["payload"]
    assert payload == {"submission_id": "sub-1", "task_id": "sub-abc"}
