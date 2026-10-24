"""Report-only public uplift (ticket 02 / D1 accept vs publish)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from ultimate_coders.inference import (
    EvalRole,
    MetaInferResult,
    MetaInferTask,
    Oracle,
    OraclePolicy,
    OracleVerdict,
    require_report_role,
)
from ultimate_coders.inference.benchmark import BenchmarkRunner, BenchmarkSpec
from ultimate_coders.inference.models import (
    EVAL_ROLE_ENFORCE_ENV,
    REPORT_UPLIFT_ENFORCE_ENV,
    published_uplift_verdict,
    report_uplift_enforce_enabled,
)
from ultimate_coders.inference.workflow import OptimizationWorkflow


def _make_repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "latency.txt").write_text("43", encoding="utf-8")
    (root / "bench.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "print(json.dumps({'workload_id':'fixed', 'correctness':True, 'compile_success':True, "
        "'metrics':{'tpot_ms':float(Path('latency.txt').read_text())}}))\n",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _runner(root: Path) -> BenchmarkRunner:
    return BenchmarkRunner(
        BenchmarkSpec(
            [sys.executable, "-B", "bench.py"],
            "fixed",
            protected_paths=["bench.py"],
        )
    )


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_report_uplift_flag_falsey_is_off(monkeypatch, value):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, value)
    assert report_uplift_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_report_uplift_flag_truthy_is_on(monkeypatch, value):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, value)
    assert report_uplift_enforce_enabled() is True


def test_report_uplift_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(REPORT_UPLIFT_ENFORCE_ENV, raising=False)
    assert report_uplift_enforce_enabled() is False


def test_require_report_role_enforces_under_report_uplift_flag(monkeypatch):
    monkeypatch.delenv(EVAL_ROLE_ENFORCE_ENV, raising=False)
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    with pytest.raises(ValueError, match="report"):
        require_report_role(OracleVerdict(accepted=True, eval_role=EvalRole.SELECT))
    require_report_role(OracleVerdict(accepted=True, eval_role=EvalRole.REPORT))


def test_published_uplift_uses_select_iteration_when_flag_off(monkeypatch):
    monkeypatch.delenv(REPORT_UPLIFT_ENFORCE_ENV, raising=False)
    select_verdict = {
        "accepted": True,
        "reasons": [],
        "improvement_pct": 99.0,
        "eval_role": "select",
    }
    result = {
        "success": True,
        "iterations": [{"verdict": select_verdict}],
        "report_verdict": {
            "accepted": True,
            "reasons": [],
            "improvement_pct": 1.0,
            "eval_role": "report",
        },
    }
    assert published_uplift_verdict(result) == select_verdict


def test_published_uplift_requires_report_when_flag_on(monkeypatch):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    select_verdict = {
        "accepted": True,
        "reasons": [],
        "improvement_pct": 99.0,
        "eval_role": "select",
    }
    with pytest.raises(ValueError, match="report remasure"):
        published_uplift_verdict(
            {"success": True, "iterations": [{"verdict": select_verdict}]}
        )


def test_published_uplift_non_optimization_success_has_no_uplift(monkeypatch):
    """Analyze/benchmark success must not demand report remasure or select fallback."""
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    assert (
        published_uplift_verdict(
            {"success": True, "task_type": "analyze_trace", "backend": {}}
        )
        is None
    )
    assert (
        published_uplift_verdict(
            {"success": True, "task_type": "benchmark", "measurement": {}}
        )
        is None
    )


def test_published_uplift_rejects_select_tagged_report_field(monkeypatch):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    with pytest.raises(ValueError, match="report"):
        published_uplift_verdict(
            {
                "success": True,
                "report_verdict": {
                    "accepted": True,
                    "reasons": [],
                    "improvement_pct": 50.0,
                    "eval_role": "select",
                },
            }
        )


def test_published_uplift_accepts_report_verdict_only(monkeypatch):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    report = {
        "accepted": True,
        "reasons": [],
        "improvement_pct": 12.5,
        "eval_role": "report",
    }
    assert published_uplift_verdict(
        {
            "success": True,
            "iterations": [
                {
                    "verdict": {
                        "accepted": True,
                        "improvement_pct": 99.0,
                        "eval_role": "select",
                    }
                }
            ],
            "report_verdict": report,
        }
    ) == report


@pytest.mark.asyncio
async def test_flag_off_leaves_workflow_without_report_uplift(tmp_path, monkeypatch):
    monkeypatch.delenv(REPORT_UPLIFT_ENFORCE_ENV, raising=False)
    root = _make_repository(tmp_path)

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        _runner(root),
        propose,
        str(tmp_path / "artifacts"),
    )
    assert result["success"]
    assert "report_verdict" not in result
    assert "public_uplift" not in result
    assert result["iterations"][0]["verdict"]["eval_role"] == "select"


@pytest.mark.asyncio
async def test_flag_on_public_fields_come_from_report_remeasure(tmp_path, monkeypatch):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    monkeypatch.setenv(EVAL_ROLE_ENFORCE_ENV, "1")
    root = _make_repository(tmp_path)

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        _runner(root),
        propose,
        str(tmp_path / "artifacts"),
    )
    assert result["success"]
    assert result["iterations"][0]["verdict"]["eval_role"] == "select"
    assert result["iterations"][0]["verdict"]["accepted"] is True
    report = result["report_verdict"]
    assert report["eval_role"] == "report"
    assert report["improvement_pct"] == result["public_uplift"]["improvement_pct"]
    assert result["public_uplift"]["eval_role"] == "report"
    assert published_uplift_verdict(result) == report
    assert published_uplift_verdict(result)["improvement_pct"] != 99.0


@pytest.mark.asyncio
async def test_accept_loop_remains_select_under_report_enforce(tmp_path, monkeypatch):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    root = _make_repository(tmp_path)
    seen_roles = []

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        feedback = request.constraints.get("oracle_feedback") or {}
        best = feedback.get("best") or {}
        if best:
            seen_roles.append(best.get("eval_role"))
        return MetaInferResult("candidate", "completed")

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        _runner(root),
        propose,
        str(tmp_path / "artifacts"),
        max_iterations=1,
    )
    assert result["success"]
    assert result["optimized"]["eval_role"] == "select"
    assert all(item["verdict"]["eval_role"] == "select" for item in result["iterations"])
    assert result["report_verdict"]["eval_role"] == "report"


@pytest.mark.asyncio
async def test_remeasure_failure_does_not_publish_select_uplift(tmp_path, monkeypatch):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    root = _make_repository(tmp_path)
    inner = _runner(root)
    calls = {"n": 0}
    original = inner.measure

    async def flaky_measure(path, *, baseline=False):
        calls["n"] += 1
        # baseline + accept measure, then remasure fails
        if calls["n"] > 2:
            raise RuntimeError("report remasure hardware failed")
        return await original(path, baseline=baseline)

    inner.measure = flaky_measure  # type: ignore[method-assign]

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    with pytest.raises(RuntimeError, match="report remasure"):
        await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
            MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
            inner,
            propose,
            str(tmp_path / "artifacts"),
        )

    select_only = {
        "success": True,
        "iterations": [
            {
                "verdict": {
                    "accepted": True,
                    "improvement_pct": 13.95,
                    "eval_role": "select",
                }
            }
        ],
    }
    with pytest.raises(ValueError, match="report remasure"):
        published_uplift_verdict(select_only)
