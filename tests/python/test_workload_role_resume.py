"""Workload role provenance in the experiment checkpoint (D11 / T27).

D11 moved the eval role onto the workload. Two consequences are covered here:

* a ``report_benchmark`` that does not declare ``report`` is refused up front,
  instead of failing later inside the remeasure as a mint error;
* a checkpoint records the workload role that produced its numbers, so a run
  cannot resume an experiment under a different role -- and a legacy checkpoint
  (no recorded role) can only be resumed by a ``select`` run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from ultimate_coders.inference import MetaInferResult, MetaInferTask, Oracle, OraclePolicy
from ultimate_coders.inference.benchmark import BenchmarkRunner, BenchmarkSpec
from ultimate_coders.inference.workflow import OptimizationWorkflow


def _make_repository(tmp_path: Path) -> Path:
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


def _runner(*, role: str = "select") -> BenchmarkRunner:
    return BenchmarkRunner(
        BenchmarkSpec(
            [sys.executable, "-B", "bench.py"],
            "fixed",
            protected_paths=["bench.py"],
            eval_role=role,
        )
    )


def _task(root: Path) -> MetaInferTask:
    return MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang")


def _propose(root: Path):
    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        return MetaInferResult("candidate", "completed")

    return propose


async def test_a_report_runner_must_declare_the_report_role(tmp_path):
    root = _make_repository(tmp_path)
    workflow = OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms")))
    with pytest.raises(ValueError, match="must declare eval_role=report"):
        await workflow.run(
            _task(root),
            _runner(),
            _propose(root),
            str(tmp_path / "artifacts"),
            report_benchmark=_runner(),  # select-declared
        )


async def test_resume_refuses_a_workload_role_switch(tmp_path):
    root = _make_repository(tmp_path)
    artifacts = str(tmp_path / "artifacts")
    workflow = OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms")))
    await workflow.run(_task(root), _runner(), _propose(root), artifacts, max_iterations=1)
    record = json.loads((Path(artifacts) / "checkpoint.json").read_text(encoding="utf-8"))
    assert record["eval_role"] == "select"

    with pytest.raises(ValueError, match="measured under eval_role"):
        await workflow.run(
            _task(root), _runner(role="report"), _propose(root), artifacts, max_iterations=1
        )


async def test_a_legacy_checkpoint_cannot_be_resumed_as_report(tmp_path):
    root = _make_repository(tmp_path)
    artifacts = str(tmp_path / "artifacts")
    workflow = OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms")))
    await workflow.run(_task(root), _runner(), _propose(root), artifacts, max_iterations=1)

    checkpoint_path = Path(artifacts) / "checkpoint.json"
    record = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    del record["eval_role"]
    checkpoint_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="no recorded eval_role"):
        await workflow.run(
            _task(root), _runner(role="report"), _propose(root), artifacts, max_iterations=1
        )


async def test_a_legacy_checkpoint_still_resumes_a_select_run(tmp_path):
    root = _make_repository(tmp_path)
    artifacts = str(tmp_path / "artifacts")
    workflow = OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms")))
    await workflow.run(_task(root), _runner(), _propose(root), artifacts, max_iterations=1)

    checkpoint_path = Path(artifacts) / "checkpoint.json"
    record = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    del record["eval_role"]
    checkpoint_path.write_text(json.dumps(record), encoding="utf-8")

    result = await workflow.run(_task(root), _runner(), _propose(root), artifacts, max_iterations=1)
    assert result["success"]
    assert result["optimized"]["eval_role"] == "select"
