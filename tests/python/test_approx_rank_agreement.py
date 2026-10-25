"""Approx rank-agreement gate and arming latch (ticket 07 / D4)."""

from __future__ import annotations

import math

import pytest
from ultimate_coders.inference import (
    APPROX_EVAL_ARMED_ENV,
    APPROX_EVAL_ENFORCE_ENV,
    DEFAULT_SPEARMAN_THRESHOLD,
    BenchmarkResult,
    EvalRole,
    RankAgreementOutcome,
    approx_eval_armed,
    approx_eval_enforce_enabled,
    approx_eval_ready_to_prune,
    arm_approx_eval,
    disarm_approx_eval,
    evaluate_rank_agreement,
)
from ultimate_coders.inference.models import EVAL_ROLE_ENFORCE_ENV


def _result(*, role: EvalRole = EvalRole.REPORT, tpot: float = 40.0) -> BenchmarkResult:
    return BenchmarkResult(
        workload_id="fixed",
        correctness=True,
        compile_success=True,
        metrics={"tpot_ms": tpot},
        statistics={"count": 3, "dispersion_pct": {"tpot_ms": 1.0}},
        environment_id="fixture",
        eval_role=role,
    )


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_enforce_flag_falsey_is_off(monkeypatch, value):
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, value)
    assert approx_eval_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_enforce_flag_truthy_is_on(monkeypatch, value):
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, value)
    assert approx_eval_enforce_enabled() is True


def test_flags_default_off(monkeypatch):
    monkeypatch.delenv(APPROX_EVAL_ENFORCE_ENV, raising=False)
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    assert approx_eval_enforce_enabled() is False
    assert approx_eval_armed() is False
    assert approx_eval_ready_to_prune() is False


def test_default_spearman_threshold_is_explicit():
    assert DEFAULT_SPEARMAN_THRESHOLD == 0.8


def test_perfect_rank_correlation_passes():
    truth = [1.0, 2.0, 3.0, 4.0]
    approx = [10.0, 20.0, 30.0, 40.0]
    outcome = evaluate_rank_agreement(truth, approx)
    assert isinstance(outcome, RankAgreementOutcome)
    assert outcome.metric == "spearman"
    assert outcome.threshold == DEFAULT_SPEARMAN_THRESHOLD
    assert outcome.n == 4
    assert outcome.coefficient == pytest.approx(1.0)
    assert outcome.passed is True
    assert outcome.reasons == []


def test_perfect_correlation_arms_latch(monkeypatch):
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    outcome = evaluate_rank_agreement([1, 2, 3, 4], [1, 2, 3, 4])
    assert outcome.passed is True
    assert arm_approx_eval(outcome) is True
    assert approx_eval_armed() is True


def test_anti_correlated_fails_closed():
    outcome = evaluate_rank_agreement([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0])
    assert outcome.passed is False
    assert outcome.coefficient == pytest.approx(-1.0)
    assert outcome.reasons
    assert "spearman" in outcome.reasons[0]


def test_short_sequence_fails_closed():
    outcome = evaluate_rank_agreement([1.0, 2.0], [1.0, 2.0])
    assert outcome.passed is False
    assert outcome.n == 2
    assert math.isnan(outcome.coefficient)
    assert any("n=2" in r for r in outcome.reasons)


def test_mismatched_lengths_fail_closed():
    outcome = evaluate_rank_agreement([1.0, 2.0, 3.0], [1.0, 2.0])
    assert outcome.passed is False
    assert math.isnan(outcome.coefficient)
    assert any("length mismatch" in r for r in outcome.reasons)


def test_non_finite_fails_closed():
    outcome = evaluate_rank_agreement([1.0, 2.0, float("nan")], [1.0, 2.0, 3.0])
    assert outcome.passed is False
    assert any("non-finite" in r for r in outcome.reasons)


def test_constant_sequence_fails_closed():
    outcome = evaluate_rank_agreement([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])
    assert outcome.passed is False
    assert math.isnan(outcome.coefficient)
    assert any("constant" in r for r in outcome.reasons)


def test_unsupported_metric_fails_closed():
    outcome = evaluate_rank_agreement([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], metric="kendall")
    assert outcome.passed is False
    assert any("unsupported metric" in r for r in outcome.reasons)


def test_enforce_alone_does_not_imply_ready_to_prune(monkeypatch):
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, "1")
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    assert approx_eval_enforce_enabled() is True
    assert approx_eval_armed() is False
    assert approx_eval_ready_to_prune() is False


def test_ready_to_prune_requires_enforce_and_armed(monkeypatch):
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, "1")
    monkeypatch.setenv(APPROX_EVAL_ARMED_ENV, "1")
    assert approx_eval_ready_to_prune() is True
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, "0")
    assert approx_eval_ready_to_prune() is False


def test_failed_agreement_disarms(monkeypatch):
    monkeypatch.setenv(APPROX_EVAL_ARMED_ENV, "1")
    outcome = evaluate_rank_agreement([1, 2, 3], [3, 2, 1])
    assert outcome.passed is False
    assert arm_approx_eval(outcome) is False
    assert approx_eval_armed() is False


def test_arm_then_disarm(monkeypatch):
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    outcome = evaluate_rank_agreement([1, 2, 3, 4], [2, 4, 6, 8])
    assert arm_approx_eval(outcome) is True
    assert approx_eval_armed() is True
    disarm_approx_eval()
    assert approx_eval_armed() is False


def test_tie_aware_spearman_still_passes_monotonic():
    # Ties use average ranks; both sequences share the same tie pattern → ρ=1.
    truth = [1.0, 2.0, 2.0, 4.0]
    approx = [10.0, 20.0, 20.0, 40.0]
    outcome = evaluate_rank_agreement(truth, approx)
    assert outcome.passed is True
    assert outcome.coefficient == pytest.approx(1.0)


def test_truth_results_reject_non_report_when_eval_role_enforced(monkeypatch):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, "1")
    truth = [1.0, 2.0, 3.0]
    approx = [1.0, 2.0, 3.0]
    results = [
        _result(role=EvalRole.SELECT, tpot=1.0),
        _result(role=EvalRole.REPORT, tpot=2.0),
        _result(role=EvalRole.REPORT, tpot=3.0),
    ]
    outcome = evaluate_rank_agreement(truth, approx, truth_results=results)
    assert outcome.passed is False
    assert any("report identity" in r for r in outcome.reasons)


def test_truth_results_accept_report_when_eval_role_enforced(monkeypatch):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, "1")
    truth = [1.0, 2.0, 3.0]
    approx = [1.0, 2.0, 3.0]
    results = [_result(role=EvalRole.REPORT, tpot=v) for v in truth]
    outcome = evaluate_rank_agreement(truth, approx, truth_results=results)
    assert outcome.passed is True
