"""Proactive evaluation: deterministic, evidence-first, and never in ``off`` mode.

The acceptance pinned here:

* default mode ``off`` evaluates nothing;
* ``observe`` records but never proposes;
* a candidate without evidence ids is refused, not emitted;
* the same context produces the same result (deterministic by default);
* an injected ``judge`` may only move ``confidence`` -- it cannot invent a
  proposal, and a failing judge leaves the rule's confidence alone;
* an exhausted task budget suppresses the evaluation instead of proposing spend.
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.progress import ProgressVerdict
from ultimate_coders.budget import BudgetLimits, BudgetVerdict
from ultimate_coders.proactive.evaluator import (
    MAX_PROPOSALS_PER_RULE,
    PROPOSAL_KIND_TOKENS,
    EvaluationResult,
    ProactiveEvaluator,
    Proposal,
    ProposalKind,
)
from ultimate_coders.proactive.modes import ENV_PROACTIVE_MODE, ProactiveMode

_KINDS_IN_RULE_ORDER = (
    "fix_failing_ci",
    "unblock_stalled",
    "investigate_alert",
    "discovered_followup",
    "maintenance_debt",
)


def _context(**overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        "case": {"id": "case-1", "status": "active"},
        "task": {"id": "task-1", "status": "in_progress", "project_id": "proj"},
        "ci": {
            "run_id": "run-42",
            "conclusion": "failure",
            "workflow": "ci",
            "head_sha": "abc123",
        },
        "progress": {
            "action": "stop",
            "stalled_streak": 2,
            "reason": "no new evidence",
            "signature": "boom",
            "subtask_id": "s1",
        },
        "alerts": [{"id": "7", "severity": "high", "summary": "error rate up"}],
        "followups": [
            {
                "id": "abc",
                "title": "Add retry to poller",
                "detail": "flaky test",
                "request": "Add a bounded retry to the poller.",
            }
        ],
        "maintenance": [{"id": "debt-1", "title": "Drop py38 shim", "paths": ["b.py", "a.py"]}],
        "budget": {
            "admitted": True,
            "reason": "within_budget",
            "tokens_used": 10,
            "limits": {"tokens": 1000},
        },
    }
    context.update(overrides)
    return context


def test_default_mode_is_off_and_evaluates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PROACTIVE_MODE, raising=False)
    result = ProactiveEvaluator().evaluate(_context())
    assert isinstance(result, EvaluationResult)
    assert result.mode is ProactiveMode.OFF
    assert result.evaluated is False
    assert result.proposals == ()
    assert result.observed == ()
    assert result.refusals == ()
    assert result.evidence_ids == ()
    assert result.to_dict()["mode"] == "off"


def test_observe_mode_records_but_never_proposes() -> None:
    result = ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.OBSERVE)
    assert result.evaluated is True
    assert result.proposals == (), "observe mode must never surface an actionable proposal"
    assert result.observed, "observe mode is the measurement mode: it must record"
    assert all(proposal.evidence for proposal in result.observed)
    assert result.to_dict()["proposals"] == []
    assert len(result.to_dict()["observed"]) == len(result.observed)


def test_propose_mode_emits_every_rule_with_evidence() -> None:
    result = ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.PROPOSE)
    assert result.observed == ()
    kinds = [proposal.kind.value for proposal in result.proposals]
    assert kinds == list(_KINDS_IN_RULE_ORDER)
    for proposal in result.proposals:
        assert proposal.evidence, f"{proposal.kind.value} must carry evidence"
        assert all(isinstance(item, str) and item for item in proposal.evidence)
        assert 0.0 <= proposal.confidence <= 1.0
        assert proposal.title and proposal.rationale
        assert proposal.proposed_request and proposal.proposed_request.endswith(".")
        assert isinstance(proposal.estimated_tokens, int) and proposal.estimated_tokens > 0
        assert proposal.case_id == "case-1"
        assert proposal.project_id == "proj"


def test_evidence_ids_are_the_stable_fact_ids() -> None:
    result = ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.PROPOSE)
    assert result.evidence_ids == (
        "ci:run-42",
        "stall:task-1:2",
        "alert:7",
        "followup:abc",
        "debt:debt-1",
    )


def test_proposal_without_evidence_is_refused() -> None:
    context = {"followups": [{"title": "No id, no evidence"}]}
    result = ProactiveEvaluator().evaluate(context, mode=ProactiveMode.PROPOSE)
    assert result.evaluated is True
    assert result.proposals == (), "a proposal without evidence must not be emitted"
    assert len(result.refusals) == 1
    refusal = result.refusals[0]
    assert refusal.reason == "proposal_without_evidence"
    assert refusal.kind == "discovered_followup"
    assert result.evidence_ids == ()
    assert result.to_dict()["refusals"][0]["reason"] == "proposal_without_evidence"


def test_evidence_less_alert_is_refused_too() -> None:
    result = ProactiveEvaluator().evaluate(
        {"alerts": [{"severity": "critical", "summary": "p99 regression"}]},
        mode=ProactiveMode.PROPOSE,
    )
    assert result.proposals == ()
    assert [refusal.reason for refusal in result.refusals] == ["proposal_without_evidence"]


def test_evaluation_is_deterministic() -> None:
    evaluator = ProactiveEvaluator()
    first = evaluator.evaluate(_context(), mode=ProactiveMode.PROPOSE)
    second = evaluator.evaluate(_context(), mode=ProactiveMode.PROPOSE)
    assert first.to_dict() == second.to_dict()
    assert first.evidence_ids == second.evidence_ids
    # A fresh instance must agree: no hidden clock, counter or random seed.
    assert ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.PROPOSE).to_dict() == (
        first.to_dict()
    )


def test_judge_may_only_refine_confidence() -> None:
    base = ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.PROPOSE)
    judged = ProactiveEvaluator(judge=lambda proposal, context: 0.9).evaluate(
        _context(), mode=ProactiveMode.PROPOSE
    )
    assert [proposal.kind for proposal in judged.proposals] == [
        proposal.kind for proposal in base.proposals
    ]
    assert [proposal.evidence for proposal in judged.proposals] == [
        proposal.evidence for proposal in base.proposals
    ]
    assert all(proposal.confidence == 0.9 for proposal in judged.proposals)
    assert judged.evidence_ids == base.evidence_ids


def test_judge_cannot_invent_a_proposal() -> None:
    invented = Proposal(
        kind=ProposalKind.MAINTENANCE_DEBT,
        title="invented",
        rationale="invented",
        confidence=0.99,
        evidence=("made:up",),
    )
    judged = ProactiveEvaluator(judge=lambda proposal, context: invented).evaluate(
        _context(), mode=ProactiveMode.PROPOSE
    )
    assert all(proposal.title != "invented" for proposal in judged.proposals)
    assert "made:up" not in judged.evidence_ids


@pytest.mark.parametrize(
    "answer, expected",
    [
        (None, None),
        (0.25, 0.25),
        (5.0, 1.0),
        (-2.0, 0.0),
        ({"confidence": 0.75}, 0.75),
        ({"confidence": "high"}, None),
        ("0.5", None),
        (True, None),
        (float("nan"), 0.0),
    ],
)
def test_judge_answers_are_clamped_or_ignored(answer: object, expected: float | None) -> None:
    base = ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.PROPOSE)
    judged = ProactiveEvaluator(judge=lambda proposal, context: answer).evaluate(
        _context(), mode=ProactiveMode.PROPOSE
    )
    if expected is None:
        assert [proposal.confidence for proposal in judged.proposals] == [
            proposal.confidence for proposal in base.proposals
        ]
    else:
        assert all(proposal.confidence == expected for proposal in judged.proposals)


def test_a_failing_judge_keeps_the_rule_confidence() -> None:
    def judge(proposal: Proposal, context: object) -> float:
        raise RuntimeError("judge endpoint down")

    base = ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.PROPOSE)
    judged = ProactiveEvaluator(judge=judge).evaluate(_context(), mode=ProactiveMode.PROPOSE)
    assert [proposal.confidence for proposal in judged.proposals] == [
        proposal.confidence for proposal in base.proposals
    ]
    assert len(judged.proposals) == len(base.proposals)


def test_exhausted_budget_suppresses_the_evaluation() -> None:
    context = _context(budget={"admitted": False, "reason": "tokens", "tokens_used": 1000})
    result = ProactiveEvaluator().evaluate(context, mode=ProactiveMode.PROPOSE)
    assert result.evaluated is True
    assert result.blocked_by == "budget_exhausted"
    assert result.proposals == ()
    assert result.observed == ()
    assert result.refusals == ()
    assert result.to_dict()["blocked_by"] == "budget_exhausted"


def test_missing_budget_does_not_block() -> None:
    context = _context()
    context.pop("budget")
    result = ProactiveEvaluator().evaluate(context, mode=ProactiveMode.PROPOSE)
    assert result.blocked_by is None
    assert result.proposals


def test_budget_verdict_dataclass_is_accepted_as_context() -> None:
    budget = BudgetVerdict(
        admitted=False,
        reason="tokens",
        limits=BudgetLimits(tokens=100),
        tokens_used=100,
    )
    result = ProactiveEvaluator().evaluate(_context(budget=budget), mode=ProactiveMode.PROPOSE)
    assert result.blocked_by == "budget_exhausted"


def test_progress_verdict_dataclass_is_accepted_as_context() -> None:
    verdict = ProgressVerdict("stop", 2, "no new evidence: same signature", "boom")
    result = ProactiveEvaluator().evaluate(
        {"task": {"id": "task-9"}, "progress": verdict},
        mode=ProactiveMode.PROPOSE,
    )
    assert [proposal.kind.value for proposal in result.proposals] == ["unblock_stalled"]
    assert result.evidence_ids == ("stall:task-9:2",)


def test_stall_rule_respects_the_progress_stall_limit() -> None:
    below = ProactiveEvaluator(stall_limit=2).evaluate(
        {"task": {"id": "task-1"}, "progress": {"action": "retry", "stalled_streak": 1}},
        mode=ProactiveMode.PROPOSE,
    )
    assert below.proposals == ()
    at_limit = ProactiveEvaluator(stall_limit=2).evaluate(
        {"task": {"id": "task-1"}, "progress": {"action": "retry", "stalled_streak": 2}},
        mode=ProactiveMode.PROPOSE,
    )
    assert [proposal.kind.value for proposal in at_limit.proposals] == ["unblock_stalled"]
    stop = ProactiveEvaluator(stall_limit=2).evaluate(
        {"task": {"id": "task-1"}, "progress": {"action": "stop", "stalled_streak": 1}},
        mode=ProactiveMode.PROPOSE,
    )
    assert [proposal.kind.value for proposal in stop.proposals] == ["unblock_stalled"]


def test_a_noisy_context_is_bounded_per_rule() -> None:
    alerts = [{"id": str(index), "severity": "high", "summary": "boom"} for index in range(10)]
    result = ProactiveEvaluator().evaluate({"alerts": alerts}, mode=ProactiveMode.PROPOSE)
    assert len(result.proposals) == MAX_PROPOSALS_PER_RULE


def test_duplicate_facts_are_emitted_once() -> None:
    alerts = [{"id": "7", "severity": "high", "summary": "boom"}] * 3
    result = ProactiveEvaluator().evaluate({"alerts": alerts}, mode=ProactiveMode.PROPOSE)
    assert len(result.proposals) == 1
    assert result.evidence_ids == ("alert:7",)


def test_no_facts_means_no_proposals() -> None:
    result = ProactiveEvaluator().evaluate({"case": {"id": "case-1"}}, mode=ProactiveMode.PROPOSE)
    assert result.evaluated is True
    assert result.proposals == ()
    assert result.refusals == ()
    assert result.evidence_ids == ()


def test_non_mapping_context_is_rejected() -> None:
    with pytest.raises(TypeError):
        ProactiveEvaluator().evaluate(["not", "a", "context"], mode=ProactiveMode.PROPOSE)  # type: ignore[arg-type]


def test_proposal_submit_payload_shape() -> None:
    result = ProactiveEvaluator().evaluate(_context(), mode=ProactiveMode.PROPOSE)
    payload = result.proposals[0].to_submit_payload(
        principal_kind="agent", principal_id="proactive-evaluator"
    )
    assert payload["principal_kind"] == "agent"
    assert payload["principal_id"] == "proactive-evaluator"
    assert payload["kind"] in PROPOSAL_KIND_TOKENS
    assert isinstance(payload["evidence"], list) and payload["evidence"]
    for key in ("title", "rationale", "confidence", "proposed_request", "estimated_tokens"):
        assert key in payload
    with pytest.raises(ValueError):
        result.proposals[0].to_submit_payload(principal_kind=" ", principal_id="x")


def test_proposal_to_dict_omits_unset_optionals() -> None:
    proposal = Proposal(
        kind=ProposalKind.MAINTENANCE_DEBT,
        title="t",
        rationale="r",
        confidence=0.3,
        evidence=("debt:1",),
    )
    payload = proposal.to_dict()
    assert payload["evidence"] == ["debt:1"]
    assert payload["kind"] == "maintenance_debt"
    assert "case_id" not in payload
    assert "proposed_request" not in payload
    assert "project_id" not in payload
    assert "estimated_tokens" not in payload
