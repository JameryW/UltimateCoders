"""Headless domain runner, invoked through UC's sandbox process group."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Any

from .adapter import MetaInferAdapter
from .benchmark import BenchmarkRunner, BenchmarkSpec, run_command
from .graph import ExecutionAdaptationGraph
from .models import InferenceTaskType, MetaInferTask
from .oracle import Oracle, OraclePolicy
from .workflow import OptimizationWorkflow


def _task_for_workspace(data: dict[str, Any], cwd: str) -> MetaInferTask:
    fields = dict(data)
    declared_root = fields.get("repository")
    fields["repository"] = cwd
    parameters = dict(fields.get("parameters") or {})
    if fields["task_type"] == "port_model":
        # Never let a stale submitted path escape the UC-assigned worktree.
        parameters["target_framework_dir"] = cwd
    if "kernel_file_path" in parameters:
        kernel = Path(parameters["kernel_file_path"])
        if kernel.is_absolute() and declared_root:
            kernel = kernel.relative_to(Path(declared_root))
        resolved = (Path(cwd) / kernel).resolve()
        resolved.relative_to(Path(cwd).resolve())
        parameters["kernel_file_path"] = str(resolved)
    fields["parameters"] = parameters
    return MetaInferTask.from_dict(fields)


async def run_inference(request: dict[str, Any]) -> dict[str, Any]:
    cwd = str(Path(request["cwd"]).resolve())
    artifact_root = Path(
        os.environ.get("UC_INFERENCE_ARTIFACT_DIR")
        or str(Path(cwd).parent / ".uc-inference-artifacts")
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = tempfile.mkdtemp(prefix="experiment-", dir=artifact_root)
    try:
        return await _execute_inference(request, artifact_dir)
    except BaseException as exc:
        report = Path(artifact_dir) / "report.json"
        report.write_text(
            json.dumps({"success": False, "error": str(exc), "error_type": type(exc).__name__}),
            encoding="utf-8",
        )
        message = f"{type(exc).__name__}: {exc}; report={report}"
        if isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError(message) from exc
        raise RuntimeError(message) from exc


async def _execute_inference(request: dict[str, Any], artifact_dir: str) -> dict[str, Any]:
    config, cwd = request["config"], str(Path(request["cwd"]).resolve())
    task = _task_for_workspace(config["inference_task"], cwd)
    artifacts = Path(artifact_dir)
    benchmark_config = config.get("benchmark")
    benchmark = BenchmarkRunner(BenchmarkSpec(**benchmark_config)) if benchmark_config else None
    if task.task_type == InferenceTaskType.BENCHMARK:
        if benchmark is None:
            raise ValueError("benchmark configuration is required")
        benchmark.protect(cwd)
        measurement = await benchmark.measure(cwd)
        policy = OraclePolicy.for_task(task, config.get("oracle"))
        reasons = Oracle(policy).validate(measurement)
        result = {
            "success": not reasons,
            "task_type": task.task_type.value,
            "measurement": measurement.to_dict(),
            "reasons": reasons,
        }
    else:
        url = os.environ.get("UC_METAINFER_URL", "")
        adapter = MetaInferAdapter(url, timeout_seconds=request.get("timeout_seconds", 3600))

        async def propose(candidate: MetaInferTask):
            backend = await adapter.execute(candidate)
            apply_command = config.get("apply_command")
            if apply_command:
                await run_command(
                    apply_command,
                    cwd,
                    timeout_seconds=request.get("timeout_seconds", 300),
                    env={
                        **os.environ,
                        "UC_METAINFER_WORKSPACE": backend.artifacts.get("workspace_dir", ""),
                        "UC_METAINFER_TASK_ID": backend.task_id,
                    },
                )
            return backend

        if task.task_type == InferenceTaskType.ANALYZE_TRACE:
            # Analysis cannot claim patch acceptance or a performance improvement.
            backend = await adapter.execute(task)
            graph = ExecutionAdaptationGraph.for_task(task)
            graph.record_evidence(1, {"backend": backend.artifacts})
            result = {
                "success": True,
                "task_type": task.task_type.value,
                "backend": backend.to_dict(),
                "graph": graph.to_dict(),
            }
        else:
            if benchmark is None:
                raise ValueError(
                    "Optimization requires benchmark configuration and protected_paths"
                )
            workflow = OptimizationWorkflow(
                Oracle(OraclePolicy.for_task(task, config.get("oracle")))
            )
            result = await workflow.run(
                task,
                benchmark,
                propose,
                artifact_dir,
                max_iterations=config.get("max_iterations", 1),
            )
    if "graph" in result:
        (artifacts / "graph.json").write_text(
            json.dumps(result["graph"], indent=2), encoding="utf-8"
        )
    result.setdefault("artifacts", {})["report"] = str(artifacts / "report.json")
    (artifacts / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def _main(request: dict[str, Any]) -> int:
    os.environ.setdefault("UC_INFERENCE_OWNER_PID", str(os.getpid()))
    current = asyncio.current_task()
    handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda *_: current.cancel())

    async def watch_cancel() -> None:
        while True:
            if Path(request["cancel_file"]).exists():
                current.cancel()
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(watch_cancel()) if request.get("cancel_file") else None
    try:
        result = await asyncio.wait_for(
            run_inference(request), request.get("timeout_seconds", 3600)
        )
        success = result["success"] is True
        summary = (
            f"Inference {result['task_type']}: "
            f"{'accepted' if success else 'rejected'}; report={result['artifacts']['report']}"
        )
        # Full evidence is in report.json; keep the sandbox envelope bounded.
        memory_result = {
            key: value
            for key, value in result.items()
            if key not in ("iterations", "patch", "backend")
        }
        print(
            json.dumps(
                {"event": "final", "success": success, "summary": summary, "result": memory_result}
            ),
            flush=True,
        )
        return 0 if success else 1
    except (Exception, asyncio.CancelledError) as exc:
        print(
            json.dumps(
                {
                    "event": "final",
                    "success": False,
                    "summary": f"Inference execution failed: {type(exc).__name__}: {exc}",
                }
            ),
            flush=True,
        )
        return 1
    finally:
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    return asyncio.run(_main(json.loads(Path(args.request).read_text(encoding="utf-8"))))


if __name__ == "__main__":
    raise SystemExit(main())
