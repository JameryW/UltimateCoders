"""Acceptance is evidence-based, not the backend's success string."""

import pytest
from ultimate_coders.inference import BenchmarkResult, Oracle, OraclePolicy


def test_correct_patch_with_slower_tpot_is_rejected():
    baseline = BenchmarkResult("decode-bs1", True, True, {"tpot_ms": 43})
    candidate = BenchmarkResult("decode-bs1", True, True, {"tpot_ms": 78})
    verdict = Oracle(OraclePolicy(objective="tpot_ms")).evaluate(baseline, candidate)
    assert not verdict.accepted
    assert any("tpot_ms" in reason for reason in verdict.reasons)


@pytest.mark.parametrize(
    "data",
    [
        {"correctness": False},
        {"compile_success": False},
        {"workload_id": "different"},
        {"metrics": {}},
        {"metrics": {"tpot_ms": 37, "peak_memory_gb": 90}},
        {"numerical_error": 0.5},
    ],
)
def test_oracle_rejects_incomplete_or_unsafe_measurements(data):
    baseline = BenchmarkResult("fixed", True, True, {"tpot_ms": 43, "peak_memory_gb": 60}, 0.001)
    values = {
        "workload_id": "fixed",
        "correctness": True,
        "compile_success": True,
        "metrics": {"tpot_ms": 37, "peak_memory_gb": 59},
        "numerical_error": 0.001,
    }
    values.update(data)
    verdict = Oracle(OraclePolicy(max_memory_gb=80, max_numerical_error=0.01)).evaluate(
        baseline,
        BenchmarkResult.from_dict(values),
    )
    assert not verdict.accepted
    assert verdict.reasons


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "37"])
def test_invalid_metric_cannot_be_accepted(value):
    with pytest.raises(ValueError, match="finite nonnegative"):
        BenchmarkResult("fixed", True, True, {"tpot_ms": value})


def test_throughput_objective_improves_in_the_opposite_direction():
    statistics = {"count": 3, "dispersion_pct": {"throughput_tokens_s": 0}}
    baseline = BenchmarkResult("fixed", True, True, {"throughput_tokens_s": 100},
                               statistics=statistics, environment_id="fixture")
    candidate = BenchmarkResult("fixed", True, True, {"throughput_tokens_s": 125},
                                statistics=statistics, environment_id="fixture")
    verdict = Oracle(OraclePolicy("throughput_tokens_s", min_improvement_pct=20)).evaluate(
        baseline,
        candidate,
    )
    assert verdict.accepted
    assert verdict.improvement_pct == 25


def test_no_change_does_not_count_as_an_optimization():
    measured = BenchmarkResult("fixed", True, True, {"tpot_ms": 43})
    assert not Oracle().evaluate(measured, measured).accepted
