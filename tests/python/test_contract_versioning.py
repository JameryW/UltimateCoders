"""Task contract versioning: a changed goal is refused, not silently run.

The dispatch carries the original request text. Two dispatches of the same
task with different text mean the goal moved; the worker must not report
the old result as an answer to the new question.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ultimate_coders.agent.contract import ContractRegistry, contract_digest
from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.nats_worker import NatsWorker
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return RuntimeState()


# ── digest ─────────────────────────────────────────────────────────


def test_digest_ignores_cosmetic_differences_only():
    a = "Implement OAuth login.\r\nDo not touch main.   \n"
    b = "\nImplement OAuth login.\nDo not touch main.\n\n"
    assert contract_digest(a) == contract_digest(b)
    # One word flips the risk posture; it must flip the digest.
    assert contract_digest("Do not touch main.") != contract_digest("May touch main.")


# ── registry ───────────────────────────────────────────────────────


def test_first_observation_is_version_one(state):
    verdict = ContractRegistry(state).observe("t1", "do the thing")
    assert (verdict.status, verdict.version) == ("first_seen", 1)


def test_same_text_is_unchanged(state):
    registry = ContractRegistry(state)
    registry.observe("t1", "do the thing")
    verdict = registry.observe("t1", "do the thing")
    assert (verdict.status, verdict.version) == ("unchanged", 1)


def test_changed_text_bumps_the_version_and_keeps_history(state):
    registry = ContractRegistry(state)
    first = registry.observe("t1", "do the thing")
    verdict = registry.observe("t1", "do the other thing")
    assert verdict.changed
    assert verdict.version == 2
    assert verdict.previous_version == 1
    assert verdict.previous_digest == registry.get("t1")["previous_digest"]
    assert first.version == 1

    record = registry.get("t1")
    assert record["excerpt"] == "do the other thing"
    assert registry.observe("t1", "do the other thing").status == "unchanged"


# ── worker gate ────────────────────────────────────────────────────


def _worker(execute):
    worker = NatsWorker(mode="worker")
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=execute)
    worker._publisher = SimpleNamespace(publish_terminal=AsyncMock(return_value=True))
    return worker


@pytest.mark.asyncio
async def test_contract_change_refuses_execution_and_reports(state):
    execute = AsyncMock(
        return_value=SubtaskResult(subtask_id="node", summary="old work", success=True)
    )
    worker = _worker(execute)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", user_request="build login v1"),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    assert state.get("result_outbox", "graph:node")["attempt"] == 0

    # Same subtask, different contract, next attempt.
    await worker._execute_and_report_body(
        Subtask(
            id="node",
            parent_id="graph",
            user_request="build login v2 with SSO",
            dispatch_retry_count=1,
        ),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )

    execute.assert_awaited_once()  # the second dispatch ran nothing
    record = state.get("result_outbox", "graph:node")
    assert record["attempt"] == 1  # the refusal supersedes the old outcome
    assert record["event"]["data"]["contract_changed"] is True
    assert record["event"]["data"]["contract_previous_version"] == 1
    assert record["event"]["data"]["contract_version"] == 2
    assert record["event"]["data"]["retryable"] is False
    assert record["update"]["subtasks"][0]["status"] == "Failed"
    assert state.get("task_contracts", "graph")["version"] == 2


@pytest.mark.asyncio
async def test_unchanged_contract_does_not_trigger_the_gate(state):
    execute = AsyncMock(return_value=SubtaskResult(subtask_id="node", summary="done", success=True))
    worker = _worker(execute)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", user_request="fixed text"),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", user_request="fixed text", dispatch_retry_count=1),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    record = state.get("result_outbox", "graph:node")
    assert "contract_changed" not in record["event"]["data"]
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_missing_user_request_skips_the_gate(state):
    """A dispatch without a user_request carries no contract to compare.

    Sibling subtasks of one task differ in description by design; treating
    the description as the contract would flag every sibling as a change.
    """
    execute = AsyncMock(return_value=SubtaskResult(subtask_id="node", summary="done", success=True))
    worker = _worker(execute)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", description="just this subtask"),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    assert state.get("task_contracts", "graph") is None
    assert execute.await_count == 1
