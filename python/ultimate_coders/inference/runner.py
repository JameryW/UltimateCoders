"""Headless domain runner, invoked through UC's sandbox process group."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ultimate_coders.runtime_state import (
    RuntimeState,
    atomic_json,
    process_identity,
    process_matches,
)

from .adapter import MetaInferAdapter, RemoteStateUncertainError
from .artifacts import artifact_root as resolve_artifact_root
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


def _artifact_metadata(directory: Path) -> dict:
    return {
        path.name: {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for path in directory.iterdir()
        if path.is_file() and path.name != "manifest.json"
    }


async def run_inference(request: dict[str, Any]) -> dict[str, Any]:
    cwd = str(Path(request["cwd"]).resolve())
    artifact_root = resolve_artifact_root(cwd)
    artifact_root.mkdir(parents=True, exist_ok=True)
    identity = request["config"].get("uc_execution", {"local_id": uuid.uuid4().hex})
    experiment_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    artifact_dir = str(artifact_root / experiment_id)
    Path(artifact_dir).mkdir(parents=True, exist_ok=True)
    request["experiment_id"] = experiment_id
    request["identity"] = identity
    request["runtime_path"] = str(
        Path(os.environ.get("UC_RUNTIME_STATE_DIR") or Path(cwd) / ".uc/runtime") / "state.sqlite3"
    )
    state = RuntimeState(request["runtime_path"])
    owner = request.get("workspace_owner")
    if owner:
        def register_runner(lease):
            if (lease.get("claim") != owner["claim"]
                    or lease.get("status") != "active"
                    or lease.get("host") != socket.gethostname()
                    or not process_matches(lease["pid"], lease.get("process_identity"))):
                raise RemoteStateUncertainError("Workspace owner exited or lease was superseded")
            if lease.get("runner_pid") and process_matches(
                lease["runner_pid"], lease.get("runner_process_identity")
            ):
                raise RemoteStateUncertainError("Workspace already has an active runner")
            return {**lease, "runner_pid": os.getpid(), "runner_host": socket.gethostname(),
                    "runner_process_identity": process_identity(os.getpid())}

        # Atomic with worktree adoption: either this child registers before
        # takeover, or the stale child exits before executing any commands.
        await asyncio.to_thread(state.mutate, "workspace_leases", owner["lease_key"],
                                register_runner)
    config_sha = hashlib.sha256(
        json.dumps(
            {key: value for key, value in request["config"].items() if not key.startswith("_uc_")},
            sort_keys=True,
        ).encode()
    ).hexdigest()

    def register(old):
        if old.get("config_sha256", config_sha) != config_sha:
            raise RemoteStateUncertainError(
                "Experiment configuration changed; reconcile before retry"
            )
        return {
            **old,
            "config_sha256": config_sha,
            "identity": identity,
            "artifact_dir": artifact_dir,
            "workspace": cwd,
            "owner_pid": os.getpid(),
            "owner_process_identity": process_identity(os.getpid()),
            "owner_host": socket.gethostname(),
            "state": "running",
        }

    await asyncio.to_thread(
        state.mutate,
        "experiments",
        experiment_id,
        register,
    )
    try:
        return await _execute_inference(request, artifact_dir)
    except BaseException as exc:
        report = Path(artifact_dir) / "report.json"
        atomic_json(
            report,
            {
                "success": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "cleanup_pending": getattr(exc, "cleanup_pending", False),
            },
        )
        failure = {
            "experiment_id": experiment_id,
            "identity": identity,
            "config_sha256": config_sha,
            "state": "cleanup_pending" if getattr(exc, "cleanup_pending", False) else "failed",
            "artifacts": _artifact_metadata(Path(artifact_dir)),
        }
        atomic_json(Path(artifact_dir) / "manifest.json", failure)
        print(
            json.dumps(
                {
                    "event": "inference_phase",
                    "phase": failure["state"],
                    "experiment_id": experiment_id,
                }
            ),
            flush=True,
        )
        try:
            await asyncio.to_thread(
                state.mutate,
                "experiments",
                experiment_id,
                lambda old: {**old, **failure},
            )
        except Exception:
            logging.getLogger(__name__).exception("Experiment failure checkpoint unavailable")
        message = f"{type(exc).__name__}: {exc}; report={report}"
        if isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError(message) from exc
        exc.args = (message,)
        raise


async def _execute_inference(request: dict[str, Any], artifact_dir: str) -> dict[str, Any]:
    config, cwd = request["config"], str(Path(request["cwd"]).resolve())
    task = _task_for_workspace(config["inference_task"], cwd)
    artifacts = Path(artifact_dir)

    async def phase(name, **data):
        print(
            json.dumps(
                {
                    "event": "inference_phase",
                    "phase": name,
                    "experiment_id": request["experiment_id"],
                    **data,
                }
            ),
            flush=True,
        )

    state = RuntimeState(request["runtime_path"])
    iteration = 0
    benchmark_config = config.get("benchmark")
    benchmark = BenchmarkRunner(BenchmarkSpec(**benchmark_config)) if benchmark_config else None
    if task.task_type == InferenceTaskType.BENCHMARK:
        if benchmark is None:
            raise ValueError("benchmark configuration is required")
        benchmark.protect(cwd)
        await phase("benchmark")
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
        adapter = MetaInferAdapter(
            url,
            timeout_seconds=request.get("timeout_seconds", 3600),
            state=state,
            max_concurrency=int(os.environ.get("UC_METAINFER_MAX_CONCURRENCY", "1")),
        )

        async def propose(candidate: MetaInferTask):
            nonlocal iteration
            iteration = candidate.constraints.get("uc_iteration", iteration + 1)
            adapter.operation_id = f"{request['experiment_id']}:{iteration}"
            backend = await adapter.execute(candidate)
            apply_command = config.get("apply_command")
            if apply_command:
                claim = uuid.uuid4().hex
                hook = await asyncio.to_thread(
                    state.mutate,
                    "apply_hooks",
                    adapter.operation_id,
                    lambda old: old or {"state": "applying", "claim": claim},
                )
                if hook.get("state") == "applied":
                    return backend
                if hook.get("claim") != claim:
                    raise RemoteStateUncertainError(
                        "Import command outcome unknown; reconcile workspace before retry"
                    )
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
                await asyncio.to_thread(
                    state.mutate,
                    "apply_hooks",
                    adapter.operation_id,
                    lambda old: {**old, "state": "applied"},
                )
            return backend

        if task.task_type == InferenceTaskType.ANALYZE_TRACE:
            # Analysis cannot claim patch acceptance or a performance improvement.
            adapter.operation_id = f"{request['experiment_id']}:trace"
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
                on_phase=phase,
            )
    result["experiment_id"] = request["experiment_id"]
    result["acceptance_state"] = (
        "accepted" if result["success"] and result.get("patch") else "completed"
    )
    result["delivery"] = {"status": "pending" if result.get("patch") else "not_required"}
    if "graph" in result:
        atomic_json(artifacts / "graph.json", result["graph"])
    result.setdefault("artifacts", {})["report"] = str(artifacts / "report.json")
    atomic_json(artifacts / "report.json", result)
    manifest = {
        "experiment_id": request["experiment_id"],
        "identity": request["identity"],
        "task": task.to_dict(),
        "benchmark": benchmark_config,
        "oracle": config.get("oracle", {}),
        "code_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=False
        ).stdout.strip(),
        "state": "accepted" if result["success"] and result.get("patch") else "completed",
        "artifacts": _artifact_metadata(artifacts),
    }
    atomic_json(artifacts / "manifest.json", manifest)
    await asyncio.to_thread(
        state.mutate, "experiments", request["experiment_id"], lambda old: {**old, **manifest}
    )
    await phase(manifest["state"])
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
                    "retryable": getattr(exc, "retryable", True),
                    "cleanup_pending": getattr(exc, "cleanup_pending", False),
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
