"""Completion condition adapters (ticket 06 / D3): verify, step, re-eval.

Adapters live under the same ``evaluate_completion_conditions`` authority —
no second completion registry. Model/review self-approval alone cannot
satisfy ``independent_reeval``.
"""

from __future__ import annotations

from ultimate_coders.agent.contract import (
    CompletionCondition,
    evaluate_completion_conditions,
)
from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import ChangeType, FileChange
from ultimate_coders.inference.benchmark import IndependentReevalOutcome

# ── verify_command ─────────────────────────────────────────────────


def test_verify_command_passes_on_boolean_evidence():
    conditions = [CompletionCondition("verify_command", {"cmd": "pytest -q"})]
    verdict = evaluate_completion_conditions(conditions, {"verify_command_passed": True})
    assert verdict.satisfied is True
    assert verdict.results[0].passed is True
    assert "verify_command_passed" in verdict.results[0].reason
    assert "pytest" in verdict.results[0].reason


def test_verify_command_fails_without_true_flag():
    conditions = [{"type": "verify_command", "params": {"cmd": "make test"}}]
    for evidence in (
        {},
        {"verify_command_passed": False},
        {"verify_command_passed": 1},
        {"verify_command_passed": "yes"},
        {"cmd_ok": True},
    ):
        verdict = evaluate_completion_conditions(conditions, evidence)
        assert verdict.satisfied is False
        assert verdict.results[0].passed is False


# ── step_condition ─────────────────────────────────────────────────


def test_step_condition_passes_when_expr_true():
    prev = AgentOutput(summary="ok", file_changes=[], success=True)
    conditions = [CompletionCondition("step_condition", {"expr": "prev.success"})]
    verdict = evaluate_completion_conditions(conditions, {"prev": prev})
    assert verdict.satisfied is True
    assert verdict.results[0].passed is True


def test_step_condition_fails_when_expr_false_or_missing_prev():
    conditions = [CompletionCondition("step_condition", {"expr": "prev.success"})]
    assert evaluate_completion_conditions(conditions, {"prev": None}).satisfied is False
    assert evaluate_completion_conditions(conditions, {}).satisfied is False
    failed = AgentOutput(summary="x", success=False)
    assert evaluate_completion_conditions(conditions, {"prev": failed}).satisfied is False


def test_step_condition_files_contains():
    prev = AgentOutput(
        summary="touched",
        file_changes=[FileChange(file_path="src/foo.py", change_type=ChangeType.MODIFIED, diff="")],
        success=True,
    )
    conditions = [
        CompletionCondition(
            "step_condition",
            {"expr": 'prev.files.contains("foo.py")'},
        )
    ]
    assert evaluate_completion_conditions(conditions, {"prev": prev}).satisfied is True
    assert (
        evaluate_completion_conditions(
            conditions,
            {"prev": AgentOutput(success=True)},
        ).satisfied
        is False
    )


def test_step_condition_parse_error_fails_closed():
    conditions = [CompletionCondition("step_condition", {"expr": "!!!not-valid"})]
    verdict = evaluate_completion_conditions(conditions, {"prev": None})
    assert verdict.satisfied is False
    assert verdict.results[0].passed is False
    assert "error" in verdict.results[0].reason.lower()


def test_step_condition_requires_expr_string():
    verdict = evaluate_completion_conditions(
        [CompletionCondition("step_condition", {})],
        {},
    )
    assert verdict.satisfied is False
    assert "expr" in verdict.results[0].reason


def test_step_condition_empty_expr_follows_step_condition_semantics():
    """Empty / whitespace expr is always true (step_condition.evaluate contract)."""
    conditions = [CompletionCondition("step_condition", {"expr": ""})]
    assert evaluate_completion_conditions(conditions, {"prev": None}).satisfied is True
    assert (
        evaluate_completion_conditions(
            [CompletionCondition("step_condition", {"expr": "  "})],
            {},
        ).satisfied
        is True
    )


# ── independent_reeval ─────────────────────────────────────────────


def _passing_outcome() -> IndependentReevalOutcome:
    return IndependentReevalOutcome(
        passed=True,
        correctness=True,
        attestation_ok=True,
        result=None,
        reasons=[],
        eval_root="/sealed",
    )


def _failing_outcome() -> IndependentReevalOutcome:
    return IndependentReevalOutcome(
        passed=False,
        correctness=False,
        attestation_ok=False,
        result=None,
        reasons=["sealed path rejected"],
        eval_root="/agent-ws",
    )


def test_independent_reeval_passes_on_outcome():
    conditions = [CompletionCondition("independent_reeval", {})]
    verdict = evaluate_completion_conditions(conditions, {"independent_reeval": _passing_outcome()})
    assert verdict.satisfied is True
    assert verdict.results[0].passed is True


def test_independent_reeval_passes_on_boolean_flag():
    conditions = [CompletionCondition("independent_reeval", {})]
    verdict = evaluate_completion_conditions(conditions, {"independent_reeval_passed": True})
    assert verdict.satisfied is True


def test_independent_reeval_fails_on_failing_outcome_or_missing():
    conditions = [CompletionCondition("independent_reeval", {})]
    assert (
        evaluate_completion_conditions(
            conditions, {"independent_reeval": _failing_outcome()}
        ).satisfied
        is False
    )
    assert evaluate_completion_conditions(conditions, {}).satisfied is False
    assert (
        evaluate_completion_conditions(conditions, {"independent_reeval_passed": False}).satisfied
        is False
    )


def test_independent_reeval_ignores_model_and_review_self_approval():
    """Model/review self-approval alone must not satisfy the re-eval condition."""
    conditions = [CompletionCondition("independent_reeval", {})]
    evidence = {
        "approved": True,
        "done": True,
        "llm_done": True,
        "model_approved": True,
        "review_approved": True,
        "independent_reeval": _failing_outcome(),
    }
    verdict = evaluate_completion_conditions(conditions, evidence)
    assert verdict.satisfied is False
    assert verdict.results[0].passed is False


# ── review_approved (non-LLM verified only) ────────────────────────


def test_review_approved_fails_without_review_verified():
    conditions = [CompletionCondition("review_approved", {})]
    for evidence in (
        {},
        {"review_approved": True},
        {"approved": True},
        {"review_verified": False},
        {"review_verified": 1},
        {"review_verified": "yes"},
    ):
        verdict = evaluate_completion_conditions(conditions, evidence)
        assert verdict.satisfied is False
        assert (
            "review_verified" in verdict.results[0].reason
            or "alone" in verdict.results[0].reason.lower()
        )


def test_review_approved_passes_when_review_verified():
    conditions = [CompletionCondition("review_approved", {})]
    verdict = evaluate_completion_conditions(conditions, {"review_verified": True})
    assert verdict.satisfied is True


# ── no second registry ─────────────────────────────────────────────


def test_adapters_compose_under_one_evaluate():
    conditions = [
        CompletionCondition("verify_command", {}),
        CompletionCondition("independent_reeval", {}),
        CompletionCondition("evidence_flag", {"name": "tests_green"}),
    ]
    evidence = {
        "verify_command_passed": True,
        "independent_reeval": _passing_outcome(),
        "tests_green": True,
    }
    assert evaluate_completion_conditions(conditions, evidence).satisfied is True
    evidence["tests_green"] = False
    assert evaluate_completion_conditions(conditions, evidence).satisfied is False
