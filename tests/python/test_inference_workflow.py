"""Real local git workspace tests for evidence gating and rollback."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from ultimate_coders.inference import MetaInferResult, MetaInferTask, Oracle, OraclePolicy
from ultimate_coders.inference.benchmark import BenchmarkRunner, BenchmarkSpec
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


@pytest.mark.asyncio
async def test_improvement_survives_later_regression_and_feedback_is_recorded(tmp_path):
    root = _make_repository(tmp_path)
    task = MetaInferTask("optimize_runtime", str(root), "minimize TPOT", framework="sglang")
    seen = []

    async def propose(request):
        seen.append(request)
        (root / "latency.txt").write_text("37" if len(seen) == 1 else "78", encoding="utf-8")
        if len(seen) == 2:
            (root / "bad.py").write_text("bad", encoding="utf-8")
        return MetaInferResult(f"candidate-{len(seen)}", "completed")

    benchmark = BenchmarkRunner(
        BenchmarkSpec(
            [sys.executable, "-B", "bench.py"],
            "fixed",
            protected_paths=["bench.py"],
        )
    )
    workflow = OptimizationWorkflow(Oracle(OraclePolicy("tpot_ms")))
    result = await workflow.run(
        task, benchmark, propose, str(tmp_path / "artifacts"), max_iterations=2
    )
    assert result["success"]
    assert result["optimized"]["metrics"]["tpot_ms"] == 37
    assert (root / "latency.txt").read_text() == "37"
    assert not (root / "bad.py").exists()
    assert result["iterations"][1]["verdict"]["accepted"] is False
    assert "oracle_feedback" in seen[1].constraints
    graph = json.loads(Path(result["artifacts"]["graph"]).read_text())
    assert [node["kind"] for node in graph["nodes"]].count("evidence") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancel", "harness"])
async def test_failed_iteration_restores_candidate_code(tmp_path, failure):
    root = _make_repository(tmp_path)

    async def propose(request):
        (root / "latency.txt").write_text("10", encoding="utf-8")
        (root / "new.py").write_text("new", encoding="utf-8")
        if failure == "harness":
            (root / "bench.py").write_text("print('fake')", encoding="utf-8")
            return MetaInferResult("job", "completed")
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("GPU died")

    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    runner = BenchmarkRunner(
        BenchmarkSpec([sys.executable, "-B", "bench.py"], "fixed", protected_paths=["bench.py"])
    )
    with pytest.raises(expected):
        await OptimizationWorkflow(Oracle()).run(
            MetaInferTask("optimize_kernel", str(root), "faster"),
            runner,
            propose,
            str(tmp_path / "artifacts"),
        )
    assert (root / "latency.txt").read_text() == "43"
    assert not (root / "new.py").exists()
    assert "json.dumps" in (root / "bench.py").read_text()
    assert (
        subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True).stdout
        == b""
    )


@pytest.mark.asyncio
async def test_dirty_workspace_is_refused_before_backend_execution(tmp_path):
    root = _make_repository(tmp_path)
    (root / "latency.txt").write_text("44", encoding="utf-8")

    async def propose(request):
        pytest.fail("Dirty workspace reached backend")

    with pytest.raises(ValueError, match="clean git worktree"):
        await OptimizationWorkflow(Oracle()).run(
            MetaInferTask("optimize_kernel", str(root), "faster"),
            BenchmarkRunner(BenchmarkSpec([sys.executable, "bench.py"], "fixed")),
            propose,
            str(tmp_path / "artifacts"),
        )
    assert (root / "latency.txt").read_text() == "44"


@pytest.mark.asyncio
async def test_new_model_source_is_in_the_accepted_patch(tmp_path):
    root = _make_repository(tmp_path)

    async def propose(request):
        (root / "latency.txt").write_text("37", encoding="utf-8")
        (root / "new_model.py").write_text("class NewModel: pass\n", encoding="utf-8")
        return MetaInferResult("port", "completed")

    result = await OptimizationWorkflow(Oracle()).run(
        MetaInferTask("port_model", str(root), "port model"),
        BenchmarkRunner(
            BenchmarkSpec([sys.executable, "-B", "bench.py"], "fixed", protected_paths=["bench.py"])
        ),
        propose,
        str(tmp_path / "artifacts"),
    )
    assert "new_model.py" in result["patch"]
    assert "+class NewModel: pass" in result["patch"]
    assert {item["file_path"]: item["change_type"] for item in result["file_changes"]} == {
        "latency.txt": "modified",
        "new_model.py": "created",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["file_with_directory", "directory_with_file"])
async def test_rollback_restores_path_types(tmp_path, replacement):
    root = _make_repository(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "model.py").write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "source",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )

    async def propose(request):
        if replacement == "file_with_directory":
            (root / "latency.txt").unlink()
            (root / "latency.txt").mkdir()
            (root / "latency.txt" / "candidate.py").write_text("bad")
        else:
            (root / "src" / "model.py").unlink()
            (root / "src").rmdir()
            (root / "src").write_text("bad")
        raise RuntimeError("rejected candidate")

    with pytest.raises(RuntimeError, match="rejected candidate"):
        await OptimizationWorkflow(Oracle()).run(
            MetaInferTask("optimize_kernel", str(root), "faster"),
            BenchmarkRunner(
                BenchmarkSpec(
                    [sys.executable, "-B", "bench.py"], "fixed", protected_paths=["bench.py"]
                )
            ),
            propose,
            str(tmp_path / "artifacts"),
        )
    assert (root / "latency.txt").read_text() == "43"
    assert (root / "src" / "model.py").read_text() == "original"
    assert (
        subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True).stdout
        == b""
    )


@pytest.mark.asyncio
async def test_command_timeout_terminates_descendants_before_return(tmp_path):
    from ultimate_coders.inference.benchmark import run_command

    marker = tmp_path / "orphan.txt"
    child = (
        "import time; from pathlib import Path; time.sleep(1.5); "
        "Path('orphan.txt').write_text('leaked')"
    )
    parent = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(30)"
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run_command([sys.executable, "-c", parent, child], str(tmp_path), 0.4), 3
        )
    await asyncio.sleep(1.6)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_forced_sandbox_stop_terminates_nested_command_sessions(tmp_path, monkeypatch):
    import os

    from ultimate_coders.agent.sandbox import SandboxConfig, SandboxManager
    from ultimate_coders.inference.process import spawn_command

    monkeypatch.setattr("ultimate_coders.agent.sandbox.INFERENCE_CANCEL_GRACE_SECONDS", 0.2)
    registry = tmp_path / "control"
    registry.mkdir()
    child = (
        "from pathlib import Path; import time; Path('started').write_text('yes'); "
        "time.sleep(1.5); Path('leaked').write_text('yes')"
    )
    parent = (
        "import asyncio,os,sys; from ultimate_coders.inference.benchmark import run_command; "
        "os.environ.setdefault('UC_INFERENCE_OWNER_PID',str(os.getpid())); "
        "asyncio.run(run_command([sys.executable,'-c',sys.argv[1]],os.getcwd()))"
    )
    proc, tree = await spawn_command(
        [sys.executable, "-c", parent, child],
        cwd=str(tmp_path),
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "python"),
            "UC_INFERENCE_PROCESS_REGISTRY": str(registry),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    manager = SandboxManager(SandboxConfig(project_path=str(tmp_path)))

    async def wait_until_started():
        while not (tmp_path / "started").exists():
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(wait_until_started(), 5)
        await manager._start_stopping(proc, str(registry / "cancel"), tree)
        await asyncio.sleep(1.6)
        assert not (tmp_path / "leaked").exists()
    finally:
        tree.close()
        await asyncio.wait_for(proc.wait(), 2)
