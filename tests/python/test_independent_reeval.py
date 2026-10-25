"""Independent correctness re-eval at the BenchmarkRunner seam (ticket 03 / D2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from ultimate_coders.inference.benchmark import (
    BenchmarkRunner,
    BenchmarkSpec,
    IndependentReevalOutcome,
)
from ultimate_coders.inference.models import (
    INDEPENDENT_REEVAL_ENFORCE_ENV,
    BenchmarkResult,
    independent_reeval_enforce_enabled,
)


def _write_bench(path: Path, *, correctness: bool, compile_success: bool = True) -> None:
    path.write_text(
        "import json\n"
        "print(json.dumps({"
        f"'workload_id':'w','correctness':{correctness},"
        f"'compile_success':{compile_success},"
        "'metrics':{'tpot_ms':37}"
        "}))\n",
        encoding="utf-8",
    )


def _runner(protected: str = "bench.py") -> BenchmarkRunner:
    return BenchmarkRunner(
        BenchmarkSpec(
            [sys.executable, "-B", "bench.py"],
            "w",
            protected_paths=[protected],
            warmup_runs=0,
            repetitions=3,
        )
    )


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_independent_reeval_flag_falsey_is_off(monkeypatch, value):
    monkeypatch.setenv(INDEPENDENT_REEVAL_ENFORCE_ENV, value)
    assert independent_reeval_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_independent_reeval_flag_truthy_is_on(monkeypatch, value):
    monkeypatch.setenv(INDEPENDENT_REEVAL_ENFORCE_ENV, value)
    assert independent_reeval_enforce_enabled() is True


def test_independent_reeval_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(INDEPENDENT_REEVAL_ENFORCE_ENV, raising=False)
    assert independent_reeval_enforce_enabled() is False


@pytest.mark.asyncio
async def test_independent_reeval_passes_on_sealed_root(tmp_path):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    _write_bench(sealed / "bench.py", correctness=True)
    runner = _runner()
    runner.protect(str(sealed))
    outcome = await runner.independent_reeval(str(sealed))
    assert isinstance(outcome, IndependentReevalOutcome)
    assert outcome.attestation_ok is True
    assert outcome.correctness is True
    assert outcome.passed is True
    assert outcome.result is not None
    assert outcome.result.correctness is True
    assert outcome.reasons == []
    assert Path(outcome.eval_root) == sealed.resolve()


@pytest.mark.asyncio
async def test_agent_reported_cannot_flip_passed(tmp_path):
    sealed = tmp_path / "sealed"
    agent = tmp_path / "agent"
    sealed.mkdir()
    agent.mkdir()
    _write_bench(sealed / "bench.py", correctness=False)
    _write_bench(agent / "bench.py", correctness=True)
    runner = _runner()
    runner.protect(str(sealed))
    lying = BenchmarkResult(
        workload_id="w",
        correctness=True,
        compile_success=True,
        metrics={"tpot_ms": 1.0},
    )
    outcome = await runner.independent_reeval(
        str(sealed),
        agent_workspace=str(agent),
        agent_reported=lying,
    )
    assert outcome.attestation_ok is True
    assert outcome.correctness is False
    assert outcome.passed is False
    assert outcome.result is not None
    assert outcome.result.correctness is False
    assert any("correctness" in reason for reason in outcome.reasons)


@pytest.mark.asyncio
async def test_agent_reported_false_does_not_block_sealed_pass(tmp_path):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    _write_bench(sealed / "bench.py", correctness=True)
    runner = _runner()
    runner.protect(str(sealed))
    pessimistic = BenchmarkResult(
        workload_id="w",
        correctness=False,
        compile_success=True,
        metrics={"tpot_ms": 99.0},
    )
    outcome = await runner.independent_reeval(
        str(sealed),
        agent_reported=pessimistic,
    )
    assert outcome.passed is True
    assert outcome.correctness is True


@pytest.mark.asyncio
async def test_eval_root_inside_agent_workspace_fail_closes(tmp_path):
    agent = tmp_path / "agent"
    nested = agent / "candidate"
    nested.mkdir(parents=True)
    _write_bench(nested / "bench.py", correctness=True)
    runner = _runner()
    runner.protect(str(nested))
    outcome = await runner.independent_reeval(
        str(nested),
        agent_workspace=str(agent),
    )
    assert outcome.attestation_ok is False
    assert outcome.passed is False
    assert outcome.correctness is False
    assert outcome.result is None
    assert any("agent workspace" in reason.lower() for reason in outcome.reasons)


@pytest.mark.asyncio
async def test_eval_root_equal_to_agent_workspace_fail_closes(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    _write_bench(agent / "bench.py", correctness=True)
    runner = _runner()
    runner.protect(str(agent))
    outcome = await runner.independent_reeval(
        str(agent),
        agent_workspace=str(agent),
    )
    assert outcome.attestation_ok is False
    assert outcome.passed is False
    assert outcome.result is None


@pytest.mark.asyncio
async def test_unarmed_harness_fail_closes(tmp_path):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    _write_bench(sealed / "bench.py", correctness=True)
    runner = _runner()
    outcome = await runner.independent_reeval(str(sealed))
    assert outcome.attestation_ok is False
    assert outcome.passed is False
    assert outcome.correctness is False
    assert outcome.result is None
    assert any("not protected" in reason for reason in outcome.reasons)


@pytest.mark.asyncio
async def test_mismatched_harness_fail_closes(tmp_path):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    bench = sealed / "bench.py"
    _write_bench(bench, correctness=True)
    runner = _runner()
    runner.protect(str(sealed))
    bench.write_text("# tampered\n", encoding="utf-8")
    outcome = await runner.independent_reeval(str(sealed))
    assert outcome.attestation_ok is False
    assert outcome.passed is False
    assert outcome.result is None
    assert any("changed" in reason for reason in outcome.reasons)


@pytest.mark.asyncio
async def test_compile_failure_does_not_pass(tmp_path):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    _write_bench(sealed / "bench.py", correctness=True, compile_success=False)
    runner = _runner()
    runner.protect(str(sealed))
    outcome = await runner.independent_reeval(str(sealed))
    assert outcome.attestation_ok is True
    assert outcome.correctness is True
    assert outcome.passed is False
    assert any("compile" in reason for reason in outcome.reasons)


@pytest.mark.asyncio
async def test_outcome_fields_are_observable(tmp_path):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    _write_bench(sealed / "bench.py", correctness=True)
    runner = _runner()
    runner.protect(str(sealed))
    outcome = await runner.independent_reeval(str(sealed))
    assert set(outcome.__dataclass_fields__) >= {
        "passed",
        "correctness",
        "attestation_ok",
        "result",
        "reasons",
        "eval_root",
    }
