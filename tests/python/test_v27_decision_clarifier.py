from __future__ import annotations

import pytest
from ultimate_coders.knowledge.decision_clarifier import (
    DECISION_CLARIFIER_ENV,
    DecisionClarifier,
    DecisionParameter,
    GovernanceObligation,
    GovernanceSpec,
    decision_clarifier_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DECISION_CLARIFIER_ENV, raising=False)
    assert not decision_clarifier_enabled()
    monkeypatch.setenv(DECISION_CLARIFIER_ENV, "1")
    assert decision_clarifier_enabled()


def test_complete_parameters_need_no_questions() -> None:
    plan = DecisionClarifier().plan([DecisionParameter("target", 0.9, True, "where?")])
    assert plan.complete
    assert plan.questions == ()
    assert plan.risk == "none"
    assert plan.to_dict()["unresolved_impact"] == 0.0


def test_questions_are_ranked_by_impact_and_capped() -> None:
    params = [
        DecisionParameter("low", 0.4, False, "low?"),
        DecisionParameter("high", 0.9, False, "high?"),
        DecisionParameter("mid", 0.7, False, "mid?"),
    ]
    plan = DecisionClarifier(max_questions=2).plan(params)
    assert [q.name for q in plan.questions] == ["high", "mid"]
    assert plan.risk == "ask"
    assert plan.unresolved_impact == pytest.approx(2.0)


def test_low_impact_unknowns_are_assumed_not_asked() -> None:
    plan = DecisionClarifier(impact_threshold=0.5).plan(
        [DecisionParameter("minor", 0.2, False, "minor?")]
    )
    assert plan.questions == ()
    assert not plan.complete
    assert plan.risk == "assume"


def test_answers_can_be_applied() -> None:
    params = [DecisionParameter("target", 0.9, False, "where?")]
    answered = DecisionClarifier().apply_answers(params, {"target": True})
    assert answered[0].known
    assert answered[0].name == "target"


def test_invalid_parameter_and_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="impact"):
        DecisionParameter("target", 2.0, False, "where?")
    with pytest.raises(ValueError, match="question"):
        DecisionParameter("target", 0.5, False, "")
    with pytest.raises(ValueError, match="impact_threshold"):
        DecisionClarifier(1.5)


def test_enforced_obligation_requires_blocking_action() -> None:
    with pytest.raises(ValueError, match="blocking action"):
        GovernanceObligation("fairness", "no biased ranking")


def test_governance_spec_reports_compliance() -> None:
    spec = GovernanceSpec(
        [
            GovernanceObligation("fairness", "no biased ranking", "block delivery"),
            GovernanceObligation("audit", "record a receipt", "revert merge"),
        ]
    )
    report = spec.report()
    assert report.compliant
    assert report.enforced == 2
    assert report.blocking == 2
    assert spec.enforce().compliant


def test_non_enforced_rule_can_be_trace_only() -> None:
    obligation = GovernanceObligation("advisory", "prefer short names", enforced=False)
    assert not obligation.enforced
    assert GovernanceSpec([obligation]).report().compliant


def test_duplicate_obligation_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        GovernanceSpec(
            [
                GovernanceObligation("same", "one", "block"),
                GovernanceObligation("same", "two", "block"),
            ]
        )
