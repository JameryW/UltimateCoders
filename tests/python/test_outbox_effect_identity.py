"""Terminal outbox: one logical outcome per subtask, attempts supersede.

The outbox used to key records by ``task:subtask:attempt``, so every retry
minted a second terminal record and published a second terminal event for
the same subtask. These tests pin the replacement contract: the record key
is the subtask (one fact), the attempt lives inside the record, a higher
attempt supersedes a lower one, and a late finisher cannot overwrite a
newer outcome.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.nats_worker import NatsWorker
from ultimate_coders.outbox import OutcomeOutbox, outcome_fingerprint, outcome_key
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return RuntimeState()


def _event(summary: str) -> tuple[dict, dict]:
    return (
        {"type": "subtask_completed", "data": {"summary": summary}},
        {"status": "Completed", "subtasks": [{"summary": summary}]},
    )


# ── identity and fingerprint ───────────────────────────────────────


def test_outcome_key_has_no_attempt_component():
    assert outcome_key("graph", "node") == "graph:node"
    assert outcome_key("graph", "node") == outcome_key("graph", "node")


def test_fingerprint_tracks_content_not_delivery_metadata():
    event, update = _event("a")
    base = outcome_fingerprint(event, update)
    assert base == outcome_fingerprint(_event("a")[0], _event("a")[1])
    assert base != outcome_fingerprint(*_event("b"))
    # Message ids are added after hashing; they must not feed the hash.
    event["message_id"] = "outcome:x:0:abc"
    assert outcome_fingerprint(event, update) != base


# ── store semantics ────────────────────────────────────────────────


def test_first_outcome_is_stored_undelivered(state):
    outbox = OutcomeOutbox(state)
    event, update = _event("first")
    record = outbox.store(
        "graph:node",
        attempt=0,
        fingerprint=outcome_fingerprint(event, update),
        event=event,
        update=update,
    )
    assert record["attempt"] == 0
    assert record["delivered"] is False
    assert record["event"] is event


def test_higher_attempt_supersedes_and_resets_delivery(state):
    outbox = OutcomeOutbox(state)
    first_event, first_update = _event("first")
    outbox.store(
        "graph:node",
        attempt=0,
        fingerprint=outcome_fingerprint(first_event, first_update),
        event=first_event,
        update=first_update,
    )
    outbox.finish(outbox.claim("graph:node"), delivered=True)

    second_event, second_update = _event("second")
    record = outbox.store(
        "graph:node",
        attempt=1,
        fingerprint=outcome_fingerprint(second_event, second_update),
        event=second_event,
        update=second_update,
    )
    assert record["attempt"] == 1
    assert record["superseded_attempt"] == 0
    assert record["delivered"] is False
    assert record["event"]["data"]["summary"] == "second"


def test_late_finisher_cannot_overwrite_a_newer_outcome(state):
    outbox = OutcomeOutbox(state)
    newer_event, newer_update = _event("newer")
    outbox.store(
        "graph:node",
        attempt=1,
        fingerprint=outcome_fingerprint(newer_event, newer_update),
        event=newer_event,
        update=newer_update,
    )
    stale_event, stale_update = _event("stale")
    record = outbox.store(
        "graph:node",
        attempt=0,
        fingerprint=outcome_fingerprint(stale_event, stale_update),
        event=stale_event,
        update=stale_update,
    )
    assert record["attempt"] == 1
    assert record["event"]["data"]["summary"] == "newer"


def test_same_attempt_resend_keeps_delivery_state(state):
    outbox = OutcomeOutbox(state)
    event, update = _event("same")
    fp = outcome_fingerprint(event, update)
    outbox.store("graph:node", attempt=0, fingerprint=fp, event=event, update=update)
    outbox.finish(outbox.claim("graph:node"), delivered=True)

    again = outbox.store("graph:node", attempt=0, fingerprint=fp, event=event, update=update)
    assert again["delivered"] is True
    assert "superseded_attempt" not in again


def test_conflicting_result_for_one_attempt_keeps_the_first(state):
    outbox = OutcomeOutbox(state)
    a_event, a_update = _event("a")
    outbox.store(
        "graph:node",
        attempt=0,
        fingerprint=outcome_fingerprint(a_event, a_update),
        event=a_event,
        update=a_update,
    )
    b_event, b_update = _event("b")
    record = outbox.store(
        "graph:node",
        attempt=0,
        fingerprint=outcome_fingerprint(b_event, b_update),
        event=b_event,
        update=b_update,
    )
    assert record["event"]["data"]["summary"] == "a"


# ── integration through the worker body ────────────────────────────


def _worker(state, execute):
    worker = NatsWorker(mode="worker")
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=execute)
    worker._publisher = SimpleNamespace(publish_terminal=AsyncMock(return_value=True))
    return worker


@pytest.mark.asyncio
async def test_retry_supersedes_one_record_with_a_new_message_id(state):
    calls: list[int] = []

    async def execute(subtask):
        calls.append(subtask.dispatch_retry_count)
        return SubtaskResult(
            subtask_id="node", summary=f"attempt-{subtask.dispatch_retry_count}", success=True
        )

    worker = _worker(state, execute)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph"), js_msg=SimpleNamespace(ack=AsyncMock())
    )
    first = state.get("result_outbox", "graph:node")
    assert first["attempt"] == 0

    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", dispatch_retry_count=1),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    second = state.get("result_outbox", "graph:node")

    assert calls == [0, 1]
    assert second["attempt"] == 1
    assert second["superseded_attempt"] == 0
    assert first["event"]["message_id"] != second["event"]["message_id"]
    assert worker._publisher.publish_terminal.await_count == 2


@pytest.mark.asyncio
async def test_redelivery_of_an_old_attempt_neither_executes_nor_republishes(state):
    execute = AsyncMock(return_value=SubtaskResult(subtask_id="node", summary="done", success=True))
    worker = _worker(state, execute)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", dispatch_retry_count=1),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    publish_count = worker._publisher.publish_terminal.await_count

    message = SimpleNamespace(ack=AsyncMock())
    await worker._execute_and_report_body(Subtask(id="node", parent_id="graph"), js_msg=message)

    execute.assert_awaited_once()
    assert worker._publisher.publish_terminal.await_count == publish_count
    message.ack.assert_awaited_once()
    assert state.get("result_outbox", "graph:node")["attempt"] == 1
