"""eval_role identity on BenchmarkResult / OracleVerdict (ticket 01 / D1)."""

from __future__ import annotations

import pytest

from ultimate_coders.inference import (
    BenchmarkResult,
    EvalRole,
    Oracle,
    OraclePolicy,
    OracleVerdict,
    assert_report_identity,
    require_report_role,
)
from ultimate_coders.inference.models import EVAL_ROLE_ENFORCE_ENV, eval_role_enforce_enabled


def _result(**overrides) -> BenchmarkResult:
    values = {
        "workload_id": "fixed",
        "correctness": True,
        "compile_success": True,
        "metrics": {"tpot_ms": 43.0},
        "statistics": {"count": 3, "dispersion_pct": {"tpot_ms": 1.0}},
        "environment_id": "fixture",
    }
    values.update(overrides)
    return BenchmarkResult(**values)


def test_benchmark_result_defaults_to_select_role():
    result = _result()
    assert result.eval_role is EvalRole.SELECT


def test_oracle_verdict_defaults_to_select_role():
    verdict = OracleVerdict(accepted=True)
    assert verdict.eval_role is EvalRole.SELECT


def test_benchmark_result_round_trips_eval_role():
    result = _result(eval_role=EvalRole.REPORT)
    data = result.to_dict()
    assert data["eval_role"] == "report"
    restored = BenchmarkResult.from_dict(data)
    assert restored.eval_role is EvalRole.REPORT


def test_oracle_verdict_round_trips_eval_role():
    verdict = OracleVerdict(accepted=True, reasons=[], improvement_pct=10.0, eval_role=EvalRole.REPORT)
    data = verdict.to_dict()
    assert data["eval_role"] == "report"
    restored = OracleVerdict.from_dict(data)
    assert restored.eval_role is EvalRole.REPORT
    assert restored.accepted is True
    assert restored.improvement_pct == 10.0


def test_legacy_dict_missing_eval_role_defaults_to_select():
    result = BenchmarkResult.from_dict(
        {
            "workload_id": "fixed",
            "correctness": True,
            "compile_success": True,
            "metrics": {"tpot_ms": 43.0},
        }
    )
    assert result.eval_role is EvalRole.SELECT
    verdict = OracleVerdict.from_dict({"accepted": False, "reasons": ["x"]})
    assert verdict.eval_role is EvalRole.SELECT


def test_oracle_evaluate_inherits_candidate_eval_role():
    baseline = _result(eval_role=EvalRole.SELECT, metrics={"tpot_ms": 43.0})
    candidate = _result(eval_role=EvalRole.SELECT, metrics={"tpot_ms": 37.0})
    verdict = Oracle(OraclePolicy(objective="tpot_ms")).evaluate(baseline, candidate)
    assert verdict.accepted
    assert verdict.eval_role is EvalRole.SELECT


def test_oracle_accept_path_consumes_explicit_select_roles():
    baseline = _result(eval_role="select", metrics={"tpot_ms": 43.0})
    candidate = _result(eval_role="select", metrics={"tpot_ms": 37.0})
    verdict = Oracle(OraclePolicy(objective="tpot_ms")).evaluate(baseline, candidate)
    assert verdict.accepted
    assert verdict.eval_role is EvalRole.SELECT


def test_enforce_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(EVAL_ROLE_ENFORCE_ENV, raising=False)
    assert eval_role_enforce_enabled() is False
    require_report_role(_result(eval_role=EvalRole.SELECT))
    assert_report_identity(_result(eval_role=EvalRole.SEARCH))


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_enforce_flag_falsey_is_off(monkeypatch, value):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, value)
    assert eval_role_enforce_enabled() is False
    require_report_role(_result(eval_role=EvalRole.SELECT))


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_require_report_rejects_non_report_when_enforced(monkeypatch, value):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, value)
    assert eval_role_enforce_enabled() is True
    with pytest.raises(ValueError, match="report"):
        require_report_role(_result(eval_role=EvalRole.SELECT))
    with pytest.raises(ValueError, match="report"):
        assert_report_identity(
            OracleVerdict(accepted=True, eval_role=EvalRole.SEARCH)
        )


def test_require_report_accepts_report_when_enforced(monkeypatch):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, "1")
    require_report_role(_result(eval_role=EvalRole.REPORT))
    assert_report_identity(OracleVerdict(accepted=True, eval_role=EvalRole.REPORT))


def test_explicit_report_construction_allowed_when_enforced(monkeypatch):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, "1")
    result = _result(eval_role=EvalRole.REPORT)
    assert result.eval_role is EvalRole.REPORT


def test_relabel_search_or_select_to_report_refused_when_enforced(monkeypatch):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, "1")
    search = _result(eval_role=EvalRole.SEARCH)
    select = _result(eval_role=EvalRole.SELECT)
    with pytest.raises(ValueError, match="report"):
        search.with_role(EvalRole.REPORT)
    with pytest.raises(ValueError, match="report"):
        select.with_role("report")
    with pytest.raises(ValueError, match="report"):
        OracleVerdict(accepted=True, eval_role=EvalRole.SEARCH).with_role(EvalRole.REPORT)


def test_relabel_to_report_allowed_when_enforce_off(monkeypatch):
    monkeypatch.delenv(EVAL_ROLE_ENFORCE_ENV, raising=False)
    search = _result(eval_role=EvalRole.SEARCH)
    relabeled = search.with_role(EvalRole.REPORT)
    assert relabeled.eval_role is EvalRole.REPORT
    assert search.eval_role is EvalRole.SEARCH


def test_same_role_relabel_and_non_report_transitions_ok_when_enforced(monkeypatch):
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, "1")
    report = _result(eval_role=EvalRole.REPORT)
    assert report.with_role(EvalRole.REPORT).eval_role is EvalRole.REPORT
    search = _result(eval_role=EvalRole.SEARCH)
    assert search.with_role(EvalRole.SELECT).eval_role is EvalRole.SELECT


def test_invalid_eval_role_rejected():
    with pytest.raises(ValueError):
        _result(eval_role="holdout")
    with pytest.raises(ValueError):
        BenchmarkResult.from_dict(
            {
                "workload_id": "fixed",
                "correctness": True,
                "compile_success": True,
                "metrics": {"tpot_ms": 1.0},
                "eval_role": "holdout",
            }
        )
