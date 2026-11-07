"""V8 P3 / T18a — structured replan proposal only (no DAG execute).

Pinned here:

* ``UC_REPLAN_PROPOSAL_ENFORCE`` defaults off: ``action=replan`` emits nothing
  (never pretends unblock is replan); ``action=stop`` still yields unblock.
* Flag on + ``action=replan``: at most one ``ProposalKind.REPLAN``; no
  ``unblock_stalled`` for that evaluation.
* Evidence carries digest / preserve_effects ids when provided in context.
* Bridge threads replan facts under ``context["replan"]``; orchestrator passes
  the watchdog verdict as ``action`` instead of hard-coded ``stop``.
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
from ultimate_coders.proactive.evaluator import (
    REPLAN_PROPOSAL_ENFORCE_ENV,
    ProactiveEvaluator,
    ProposalKind,
    replan_proposal_enforce_enabled,
)
from ultimate_coders.proactive.modes import ProactiveMode


class FakeClient:
    def __init__(self, replies: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies = dict(replies or {})

    async def call(self, op: str, payload: dict[str, Any]) -> Any:
        self.calls.append((op, payload))
        return self._replies.get(op, {"success": True, "proposal": {"id": "prop-1"}})


def _stall_context(
    *,
    action: str = "replan",
    streak: int = 3,
    digest: str | None = "abc123",
    preserve_effects: list[str] | None = None,
) -> dict[str, Any]:
    return evaluation_context(
        task_id="task-1",
        case_id="case-1",
        stalled_streak=streak,
        action=action,
        subtask_id="sub-1",
        preserve_effects=preserve_effects if preserve_effects is not None else ["eff_push"],
        contract_digest=digest,
        replans_remaining=1,
    )


# ── flag ─────────────────────────────────────────────────────


def test_replan_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPLAN_PROPOSAL_ENFORCE_ENV, raising=False)
    assert replan_proposal_enforce_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_replan_flag_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(REPLAN_PROPOSAL_ENFORCE_ENV, raw)
    assert replan_proposal_enforce_enabled() is True


# ── evaluator ────────────────────────────────────────────────


def test_flag_off_action_replan_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPLAN_PROPOSAL_ENFORCE_ENV, raising=False)
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    result = ProactiveEvaluator().evaluate(_stall_context(), mode=ProactiveMode.PROPOSE)
    kinds = [p.kind for p in result.proposals]
    assert ProposalKind.REPLAN not in kinds
    assert ProposalKind.UNBLOCK_STALLED not in kinds
    assert result.proposals == ()


def test_flag_off_action_stop_still_unblocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPLAN_PROPOSAL_ENFORCE_ENV, raising=False)
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    ctx = evaluation_context(task_id="task-1", stalled_streak=3, action="stop")
    result = ProactiveEvaluator().evaluate(ctx, mode=ProactiveMode.PROPOSE)
    assert [p.kind for p in result.proposals] == [ProposalKind.UNBLOCK_STALLED]


def test_action_escalate_still_unblocks_below_stall_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escalate is a staircase terminal; do not silence unblock via the streak gate."""
    monkeypatch.delenv(REPLAN_PROPOSAL_ENFORCE_ENV, raising=False)
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    ctx = evaluation_context(task_id="task-1", stalled_streak=1, action="escalate")
    result = ProactiveEvaluator().evaluate(ctx, mode=ProactiveMode.PROPOSE)
    assert [p.kind for p in result.proposals] == [ProposalKind.UNBLOCK_STALLED]
    assert ProposalKind.REPLAN not in [p.kind for p in result.proposals]


def test_flag_on_replan_observe_mode_records_not_submits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REPLAN_PROPOSAL_ENFORCE_ENV, "1")
    result = ProactiveEvaluator().evaluate(_stall_context(), mode=ProactiveMode.OBSERVE)
    assert result.proposals == ()
    assert [p.kind for p in result.observed] == [ProposalKind.REPLAN]


def test_flag_on_replan_emits_replan_not_unblock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REPLAN_PROPOSAL_ENFORCE_ENV, "1")
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    result = ProactiveEvaluator().evaluate(_stall_context(), mode=ProactiveMode.PROPOSE)
    kinds = [p.kind for p in result.proposals]
    assert kinds == [ProposalKind.REPLAN]
    proposal = result.proposals[0]
    assert "replan" in proposal.title.lower()
    assert "preserve" in proposal.rationale.lower() or "eff_push" in proposal.rationale
    assert "digest:abc123" in proposal.evidence
    assert "effect:eff_push" in proposal.evidence
    assert any(e.startswith("stall:task-1:") for e in proposal.evidence)


def test_replan_evidence_omits_missing_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REPLAN_PROPOSAL_ENFORCE_ENV, "true")
    ctx = _stall_context(digest=None, preserve_effects=["eff_comment"])
    # evaluation_context omits empty digest; ensure progress has no digest key.
    assert "contract_digest" not in ctx.get("replan", {})
    result = ProactiveEvaluator().evaluate(ctx, mode=ProactiveMode.PROPOSE)
    assert len(result.proposals) == 1
    evidence = result.proposals[0].evidence
    assert not any(e.startswith("digest:") for e in evidence)
    assert "effect:eff_comment" in evidence


def test_proposal_kind_replan_token_lockstep() -> None:
    assert ProposalKind.REPLAN.value == "replan"
    assert "replan" in (
        __import__("ultimate_coders.proactive.evaluator", fromlist=["PROPOSAL_KIND_TOKENS"])
    ).PROPOSAL_KIND_TOKENS


# ── bridge ───────────────────────────────────────────────────


def test_evaluation_context_threads_replan_facts() -> None:
    context = evaluation_context(
        task_id="task-1",
        stalled_streak=2,
        action="replan",
        preserve_effects=["eff_push", "eff_comment"],
        contract_digest="deadbeef",
        replans_remaining=1,
    )
    assert context["progress"]["action"] == "replan"
    assert context["replan"] == {
        "preserve_effects": ["eff_push", "eff_comment"],
        "contract_digest": "deadbeef",
        "replans_remaining": 1,
    }
    assert context["progress"]["contract_digest"] == "deadbeef"
    assert context["progress"]["preserve_effects"] == ["eff_push", "eff_comment"]


@pytest.mark.asyncio
async def test_propose_on_stall_submits_replan_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REPLAN_PROPOSAL_ENFORCE_ENV, "1")
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient()
    outcome = await propose_on_stall(
        client,
        task_id="task-1",
        stalled_streak=3,
        action="replan",
        subtask_id="sub-1",
        preserve_effects=["eff_push"],
        contract_digest="abc123",
        replans_remaining=1,
    )
    assert outcome.submitted and outcome.submitted[0]["accepted"] is True
    op, payload = client.calls[0]
    assert op == OP_PROPOSAL_SUBMIT
    assert payload["kind"] == "replan"
    assert "digest:abc123" in payload["evidence"]
    assert "effect:eff_push" in payload["evidence"]


@pytest.mark.asyncio
async def test_propose_on_stall_replan_flag_off_submits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REPLAN_PROPOSAL_ENFORCE_ENV, raising=False)
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient()
    outcome = await propose_on_stall(
        client,
        task_id="task-1",
        stalled_streak=3,
        action="replan",
        preserve_effects=["eff_push"],
        contract_digest="abc123",
    )
    assert outcome.evaluated is True
    assert outcome.submitted == []
    assert client.calls == []


# ── orchestrator smoke ───────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_passes_replan_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REPLAN_PROPOSAL_ENFORCE_ENV, "1")
    monkeypatch.setenv("UC_PROACTIVE_MODE", "propose")
    client = FakeClient(
        replies={
            "watchdog.stall.report": {
                "verdict": "replan",
                "attempt": 1,
                "preserve_effects": ["eff_push"],
                "replans_remaining": 1,
                "attempts_remaining": 2,
            },
            OP_PROPOSAL_SUBMIT: {"success": True, "proposal": {"id": "prop-replan"}},
        }
    )
    orchestrator = Orchestrator(governance_client=client)
    stall_signal = {
        "loop_detected": True,
        "stalled_streak": 3,
        "reason": "no new evidence",
        "error_signature": "sig-abc",
        "contract_digest": "deadbeef",
    }
    outcome = SimpleNamespace(
        stall_signal=stall_signal, retryable=True, error="tests failed", retry_count=2
    )
    task = SimpleNamespace(id="task-1", case_id="case-1")
    subtask = SimpleNamespace(id="sub-1", agent_config={}, user_request="fix the flaky test")

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task, subtask, outcome) is False
    )
    ops = [op for op, _ in client.calls]
    assert ops == ["watchdog.stall.report", OP_PROPOSAL_SUBMIT]
    _, payload = client.calls[1]
    assert payload["kind"] == "replan"
    assert "digest:deadbeef" in payload["evidence"]
    assert "effect:eff_push" in payload["evidence"]
    assert isinstance(outcome.stall_signal, dict)
    assert outcome.stall_signal.get("verdict") == "replan"
    assert outcome.stall_signal.get("preserve_effects") == ["eff_push"]
