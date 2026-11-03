"""The evaluator must be reachable from the failure path — and only where it is sane.

Pinned here:

* `off` (the default) does not even evaluate; `observe` evaluates and submits
  nothing; `propose` submits through one governance op and records the answer.
* A governance refusal or an unreachable bridge is *recorded*, never raised: the
  stall path is already a failure path.
* The context carries only facts the worker already holds, and the payload sent to
  the gateway carries evidence (the gateway refuses proposals without it).
* `Orchestrator` asks the evaluator only after the staircase has stopped the
  worker — not on every failed attempt.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.proactive_bridge import (
    OP_PROPOSAL_SUBMIT,
    evaluation_context,
    propose_on_stall,
)
from ultimate_coders.proactive.modes import ProactiveMode


class FakeClient:
    """Records ops and answers from a per-op table (or raises one error)."""

    def __init__(self, replies: Any = None, *, errors: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies = dict(replies or {})
        self._errors = dict(errors or {})

    async def call(self, op: str, payload: dict[str, Any]) -> Any:
        self.calls.append((op, payload))
        if op in self._errors:
            raise self._errors[op]
        return self._replies.get(op, {"success": True, "proposal": {"id": "prop-1"}})


class GovernanceRefusalError(Exception):
    """Stand-in for `GovernanceError` (carries a machine-readable code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


STALL = {"stalled_streak": 3, "action": "stop", "task_id": "task-1", "subtask_id": "sub-1"}


def stall_signal() -> dict[str, Any]:
    return {
        "loop_detected": True,
        "stalled_streak": 3,
        "reason": "no new evidence",
        "error_signature": "sig-abc",
    }


def result() -> SimpleNamespace:
    return SimpleNamespace(
        stall_signal=stall_signal(), retryable=True, error="tests failed", retry_count=2
    )


def task() -> SimpleNamespace:
    return SimpleNamespace(id="task-1", case_id="case-1")


def subtask() -> SimpleNamespace:
    return SimpleNamespace(id="sub-1", agent_config={})


# ── context ──────────────────────────────────────────────────


def test_the_context_is_only_facts_the_worker_holds() -> None:
    context = evaluation_context(
        task_id="task-1",
        task_status="inprogress",
        project_id="proj-1",
        case_id="case-1",
        stalled_streak=3,
        subtask_id="sub-1",
    )
    assert context == {
        "task": {"id": "task-1", "status": "inprogress", "project_id": "proj-1"},
        "case": {"id": "case-1"},
        "progress": {"action": "stop", "stalled_streak": 3, "subtask_id": "sub-1"},
    }


def test_optional_facts_are_omitted_rather_than_sent_as_null() -> None:
    context = evaluation_context(task_id="task-1")
    assert context == {
        "task": {"id": "task-1"},
        "progress": {"action": "stop", "stalled_streak": 0},
    }
    assert "case" not in context
    assert "budget" not in context


# ── mode gating ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_mode_does_not_even_evaluate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_PROACTIVE_MODE", raising=False)
    client = FakeClient()
    outcome = await propose_on_stall(client, task_id="task-1", stalled_streak=3)
    assert outcome.mode is ProactiveMode.OFF
    assert outcome.evaluated is False
    assert client.calls == [], "off must not talk to the gateway at all"


@pytest.mark.asyncio
async def test_observe_mode_evaluates_but_submits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UC_PROACTIVE_MODE", "observe")
    client = FakeClient()
    outcome = await propose_on_stall(
        client, task_id="task-1", case_id="case-1", stalled_streak=3, subtask_id="sub-1"
    )
    assert outcome.evaluated is True
    assert outcome.observed, "observe records the candidate"
    assert outcome.submitted == []
    assert client.calls == [], "observing must not create worklist items"


@pytest.mark.asyncio
async def test_propose_mode_submits_one_evidence_backed_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient()
    outcome = await propose_on_stall(
        client,
        task_id="task-1",
        task_status="inprogress",
        project_id="proj-1",
        case_id="case-1",
        stalled_streak=3,
        subtask_id="sub-1",
        principal_kind="agent",
        principal_id="uc-worker",
    )
    assert outcome.mode is ProactiveMode.PROPOSE
    assert outcome.submitted and outcome.submitted[0]["accepted"] is True
    assert outcome.submitted[0]["proposal_id"] == "prop-1"

    op, payload = client.calls[0]
    assert op == OP_PROPOSAL_SUBMIT
    assert payload["kind"] == "unblock_stalled"
    assert payload["evidence"], "the gateway refuses proposals without evidence"
    assert payload["principal_kind"] == "agent"
    assert payload["principal_id"] == "uc-worker"
    assert payload["case_id"] == "case-1"
    assert payload["project_id"] == "proj-1"
    assert "unblock" in payload["proposed_request"].lower()


@pytest.mark.asyncio
async def test_below_the_stall_threshold_nothing_is_proposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient()
    outcome = await propose_on_stall(
        client, task_id="task-1", stalled_streak=0, action="retry"
    )
    assert outcome.evaluated is True
    assert outcome.submitted == []
    assert client.calls == []


# ── failure handling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_governance_refusal_is_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient(errors={OP_PROPOSAL_SUBMIT: GovernanceRefusalError("missing_permission")})
    outcome = await propose_on_stall(client, task_id="task-1", stalled_streak=3)
    assert outcome.submitted and outcome.submitted[0]["accepted"] is False
    assert outcome.submitted[0]["code"] == "missing_permission"


@pytest.mark.asyncio
async def test_an_unreachable_bridge_does_not_break_the_stall_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient(errors={OP_PROPOSAL_SUBMIT: RuntimeError("bridge down")})
    outcome = await propose_on_stall(client, task_id="task-1", stalled_streak=3)
    assert outcome.submitted[0]["accepted"] is False


@pytest.mark.asyncio
async def test_no_client_means_no_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    outcome = await propose_on_stall(None, task_id="task-1", stalled_streak=3)
    assert outcome.evaluated is False
    assert outcome.submitted == []


# ── orchestrator wiring ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stopped_stall_produces_a_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient(
        replies={
            "watchdog.stall.report": {
                "verdict": "escalate_needs_human",
                "replans_remaining": 0,
                "attempts_remaining": 0,
            },
            OP_PROPOSAL_SUBMIT: {"success": True, "proposal": {"id": "prop-9"}},
        }
    )
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    ops = [op for op, _ in client.calls]
    assert ops == ["watchdog.stall.report", OP_PROPOSAL_SUBMIT], "verdict first, then the proposal"
    assert orchestrator.last_proactive_outcome is not None
    assert orchestrator.last_proactive_outcome.submitted[0]["proposal_id"] == "prop-9"


@pytest.mark.asyncio
async def test_off_mode_asks_only_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_PROACTIVE_MODE", raising=False)
    client = FakeClient(
        replies={
            "watchdog.stall.report": {
                "verdict": "escalate_needs_human",
                "replans_remaining": 0,
                "attempts_remaining": 0,
            }
        }
    )
    orchestrator = Orchestrator(governance_client=client)

    await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), result())

    assert [op for op, _ in client.calls] == ["watchdog.stall.report"]


@pytest.mark.asyncio
async def test_a_retry_verdict_does_not_propose(monkeypatch: pytest.MonkeyPatch) -> None:
    # The staircase is still working; there is nothing for a human to unblock yet.
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient(
        replies={
            "watchdog.stall.report": {
                "verdict": "retry_same_contract",
                "replans_remaining": 1,
                "attempts_remaining": 2,
            }
        }
    )
    orchestrator = Orchestrator(governance_client=client)
    monkeypatch.setattr(orchestrator, "_maybe_stall_agent_redispatch", lambda st, res: True)

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), result())
        is True
    )
    assert [op for op, _ in client.calls] == ["watchdog.stall.report"]
    assert orchestrator.last_proactive_outcome is None
