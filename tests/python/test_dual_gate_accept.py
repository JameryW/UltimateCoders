"""Dual-gate Oracle accept (ticket 04 / D2): attestation AND independent re-eval."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from ultimate_coders.inference import (
    MetaInferResult,
    MetaInferTask,
    Oracle,
    OraclePolicy,
    OracleVerdict,
)
from ultimate_coders.inference.benchmark import (
    BenchmarkRunner,
    BenchmarkSpec,
    IndependentReevalOutcome,
)
from ultimate_coders.inference.models import (
    DUAL_GATE_ENFORCE_ENV,
    BenchmarkResult,
    dual_gate_enforce_enabled,
)
from ultimate_coders.inference.oracle import apply_dual_gate
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


def _runner() -> BenchmarkRunner:
    return BenchmarkRunner(
        BenchmarkSpec(
            [sys.executable, "-B", "bench.py"],
            "fixed",
            protected_paths=["bench.py"],
        )
    )


def _accepted_verdict() -> OracleVerdict:
    return OracleVerdict(accepted=True, reasons=[], improvement_pct=14.0)


def _rejected_oracle() -> OracleVerdict:
    return OracleVerdict(accepted=False, reasons=["No sufficient improvement"], improvement_pct=0.0)


def _reeval(
    *, attestation_ok: bool, passed: bool, correctness: bool = True
) -> IndependentReevalOutcome:
    return IndependentReevalOutcome(
        passed=passed,
        correctness=correctness,
        attestation_ok=attestation_ok,
        result=None,
        reasons=[] if passed and attestation_ok else ["sealed measure failed"],
        eval_root="/tmp/sealed",
    )


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_dual_gate_flag_falsey_is_off(monkeypatch, value):
    monkeypatch.setenv(DUAL_GATE_ENFORCE_ENV, value)
    assert dual_gate_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_dual_gate_flag_truthy_is_on(monkeypatch, value):
    monkeypatch.setenv(DUAL_GATE_ENFORCE_ENV, value)
    assert dual_gate_enforce_enabled() is True


def test_dual_gate_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(DUAL_GATE_ENFORCE_ENV, raising=False)
    assert dual_gate_enforce_enabled() is False


def test_apply_dual_gate_both_pass_keeps_oracle_accept():
    verdict = apply_dual_gate(
        _accepted_verdict(),
        _reeval(attestation_ok=True, passed=True),
    )
    assert verdict.accepted is True
    assert not any(reason.startswith("dual_gate:") for reason in verdict.reasons)


def test_apply_dual_gate_attestation_fail_rejects_even_if_reeval_would_pass():
    # attestation_ok=False implies passed=False in the real API; gate still cites attestation.
    reeval = IndependentReevalOutcome(
        passed=True,  # adversarial / impossible combo — attestation gate wins
        correctness=True,
        attestation_ok=False,
        result=None,
        reasons=["harness changed"],
        eval_root="/tmp/sealed",
    )
    verdict = apply_dual_gate(_accepted_verdict(), reeval)
    assert verdict.accepted is False
    assert "dual_gate: attestation" in verdict.reasons
    assert "dual_gate: independent_reeval" not in verdict.reasons


def test_apply_dual_gate_reeval_fail_rejects_even_if_oracle_accepts():
    verdict = apply_dual_gate(
        _accepted_verdict(),
        _reeval(attestation_ok=True, passed=False, correctness=False),
    )
    assert verdict.accepted is False
    assert "dual_gate: independent_reeval" in verdict.reasons
    assert "dual_gate: attestation" not in verdict.reasons


def test_apply_dual_gate_agent_reported_true_but_reeval_false_rejects():
    """Agent harness self-report alone never yields accept under dual gate."""
    lying = BenchmarkResult(
        workload_id="fixed",
        correctness=True,
        compile_success=True,
        metrics={"tpot_ms": 37.0},
    )
    reeval = IndependentReevalOutcome(
        passed=False,
        correctness=False,
        attestation_ok=True,
        result=lying,  # result may exist; passed is false
        reasons=["correctness is false"],
        eval_root="/tmp/sealed",
    )
    verdict = apply_dual_gate(_accepted_verdict(), reeval)
    assert verdict.accepted is False
    assert "dual_gate: independent_reeval" in verdict.reasons


def test_apply_dual_gate_keeps_prior_oracle_reject():
    verdict = apply_dual_gate(
        _rejected_oracle(),
        _reeval(attestation_ok=True, passed=True),
    )
    assert verdict.accepted is False
    assert "No sufficient improvement" in verdict.reasons
    assert not any(reason.startswith("dual_gate:") for reason in verdict.reasons)


@pytest.mark.asyncio
async def test_flag_off_workflow_accepts_without_dual_gate(tmp_path, monkeypatch):
    monkeypatch.delenv(DUAL_GATE_ENFORCE_ENV, raising=False)
    root = _make_repository(tmp_path)
    called = {"reeval": 0}

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    runner = _runner()
    original = runner.independent_reeval

    async def tracking_reeval(*args, **kwargs):
        called["reeval"] += 1
        return await original(*args, **kwargs)

    runner.independent_reeval = tracking_reeval  # type: ignore[method-assign]

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        runner,
        propose,
        str(tmp_path / "artifacts"),
    )
    assert result["success"] is True
    assert called["reeval"] == 0
    assert "dual_gate" not in result["iterations"][0]
    assert result["iterations"][0]["verdict"]["accepted"] is True


@pytest.mark.asyncio
async def test_flag_on_both_gates_pass_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv(DUAL_GATE_ENFORCE_ENV, "1")
    root = _make_repository(tmp_path)

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        _runner(),
        propose,
        str(tmp_path / "artifacts"),
    )
    assert result["success"] is True
    record = result["iterations"][0]
    assert record["verdict"]["accepted"] is True
    assert record["dual_gate"]["attestation_ok"] is True
    assert record["dual_gate"]["passed"] is True
    assert Path(record["dual_gate"]["eval_root"]).resolve() != root.resolve()
    assert root.resolve() not in Path(record["dual_gate"]["eval_root"]).resolve().parents


@pytest.mark.asyncio
async def test_flag_on_attestation_fail_rejects(tmp_path, monkeypatch):
    monkeypatch.setenv(DUAL_GATE_ENFORCE_ENV, "1")
    root = _make_repository(tmp_path)

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    runner = _runner()

    async def bad_attestation(eval_root, *, agent_workspace=None, agent_reported=None):
        return IndependentReevalOutcome(
            passed=True,
            correctness=True,
            attestation_ok=False,
            result=None,
            reasons=["Benchmark harness changed: bench.py"],
            eval_root=str(Path(eval_root).resolve()),
        )

    runner.independent_reeval = bad_attestation  # type: ignore[method-assign]

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        runner,
        propose,
        str(tmp_path / "artifacts"),
    )
    assert result["success"] is False
    assert (root / "latency.txt").read_text() == "43"
    record = result["iterations"][0]
    assert record["verdict"]["accepted"] is False
    assert "dual_gate: attestation" in record["verdict"]["reasons"]
    assert record["dual_gate"]["attestation_ok"] is False


@pytest.mark.asyncio
async def test_flag_on_reeval_fail_rejects_despite_oracle_accept(tmp_path, monkeypatch):
    monkeypatch.setenv(DUAL_GATE_ENFORCE_ENV, "1")
    root = _make_repository(tmp_path)

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    runner = _runner()

    async def bad_reeval(eval_root, *, agent_workspace=None, agent_reported=None):
        # Agent reported improvement; sealed correctness fails.
        assert agent_reported is not None
        assert agent_reported.correctness is True
        return IndependentReevalOutcome(
            passed=False,
            correctness=False,
            attestation_ok=True,
            result=None,
            reasons=["correctness is false"],
            eval_root=str(Path(eval_root).resolve()),
        )

    runner.independent_reeval = bad_reeval  # type: ignore[method-assign]

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        runner,
        propose,
        str(tmp_path / "artifacts"),
    )
    assert result["success"] is False
    assert (root / "latency.txt").read_text() == "43"
    record = result["iterations"][0]
    assert record["verdict"]["accepted"] is False
    assert "dual_gate: independent_reeval" in record["verdict"]["reasons"]
    assert record["dual_gate"]["passed"] is False
    assert record["dual_gate"]["attestation_ok"] is True


@pytest.mark.asyncio
async def test_flag_on_sealed_root_is_outside_agent_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(DUAL_GATE_ENFORCE_ENV, "1")
    root = _make_repository(tmp_path)
    seen = {}

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    runner = _runner()
    original = runner.independent_reeval

    async def capture(eval_root, *, agent_workspace=None, agent_reported=None):
        seen["eval_root"] = Path(eval_root).resolve()
        seen["agent_workspace"] = Path(agent_workspace).resolve() if agent_workspace else None
        seen["agent_reported"] = agent_reported
        return await original(
            eval_root, agent_workspace=agent_workspace, agent_reported=agent_reported
        )

    runner.independent_reeval = capture  # type: ignore[method-assign]

    result = await OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms"))).run(
        MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang"),
        runner,
        propose,
        str(tmp_path / "artifacts"),
    )
    assert result["success"] is True
    assert seen["agent_workspace"] == root.resolve()
    assert seen["eval_root"] != root.resolve()
    assert root.resolve() not in seen["eval_root"].parents
    assert seen["agent_reported"] is not None
    assert seen["agent_reported"].metrics["tpot_ms"] == 37.0
