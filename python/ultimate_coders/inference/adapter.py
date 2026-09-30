"""Execution-only client for MetaInfer's real task/form HTTP protocol."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import InferenceTaskType, MetaInferResult, MetaInferTask, nonnegative_number

DEFAULT_TASK_TYPES = {
    InferenceTaskType.PORT_MODEL: "port-model",
    InferenceTaskType.OPTIMIZE_KERNEL: "evolve-kernel",
    # Upstream builds a purpose-built runtime. Existing-framework optimization
    # needs a service plugin, selected explicitly with upstream_type.
    InferenceTaskType.OPTIMIZE_RUNTIME: "gen-infer-framework",
    InferenceTaskType.ANALYZE_TRACE: "sglang-trace-analyze",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class MetaInferError(RuntimeError):
    """Service failure; never an implicit fallback to a generic coding agent."""


class MetaInferAdapter:
    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 3600,
        poll_interval: float = 2,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MetaInfer URL must be an HTTP(S) service URL without credentials")
        self.url = url.rstrip("/")
        self.timeout_seconds = nonnegative_number(timeout_seconds, "timeout_seconds")
        self.poll_interval = nonnegative_number(poll_interval, "poll_interval")
        if self.timeout_seconds == 0:
            raise ValueError("timeout_seconds must be positive")
        self.client = client

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        try:
            response = await client.request(method, self.url + path, timeout=15, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # No raw response bodies/URLs: a proxy may echo credential material.
            raise MetaInferError(
                f"MetaInfer {method} {path} failed ({type(exc).__name__})"
            ) from exc

    async def execute(self, task: MetaInferTask) -> MetaInferResult:
        if task.task_type == InferenceTaskType.BENCHMARK:
            raise ValueError("Use BenchmarkRunner for local immutable benchmark execution")
        task_type = task.upstream_type or DEFAULT_TASK_TYPES[task.task_type]
        if not _SAFE_ID.fullmatch(task_type):
            raise ValueError("Invalid upstream task type")
        if self.client is not None:
            return await self._execute(self.client, task, task_type)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await self._execute(client, task, task_type)

    async def _execute(
        self,
        client: httpx.AsyncClient,
        task: MetaInferTask,
        task_type: str,
    ) -> MetaInferResult:
        remote_id: str | None = None

        async def run() -> MetaInferResult:
            nonlocal remote_id
            shell = "/api/sys-shell"
            schema = await self._request(client, "GET", f"{shell}/task-types/{task_type}/schema")
            if not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
                raise MetaInferError("MetaInfer returned an invalid form schema")
            answers = dict(task.parameters)
            fields = {entry["key"]: entry for entry in schema["fields"]}
            unknown = set(answers) - fields.keys()
            if unknown:
                raise ValueError(f"Unsupported {task_type} parameters: {sorted(unknown)}")
            for key, spec in fields.items():
                if key not in answers and spec.get("default") is not None:
                    answers[key] = spec["default"]
                if spec.get("required") and answers.get(key) in (None, "", []):
                    raise ValueError(f"{task_type} requires parameter {key}")
            # Never retry POST: upstream has no submission idempotency protocol.
            created = await self._request(
                client,
                "POST",
                f"{shell}/tasks",
                json={
                    "type": task_type,
                    "label": "UC " + task.task_type.value,
                    "raw_request": json.dumps(task.to_dict(), ensure_ascii=False),
                    "answers": answers,
                },
            )
            remote_id = created.get("task_id")
            if not isinstance(remote_id, str) or not _SAFE_ID.fullmatch(remote_id):
                raise MetaInferError("MetaInfer returned an invalid task_id")
            artifacts = {
                key: created[key]
                for key in ("workspace_dir", "state_dir")
                if isinstance(created.get(key), str)
            }
            artifacts["task"] = f"{self.url}{shell}/{remote_id}"
            while True:
                state = await self._request(client, "GET", f"{shell}/{remote_id}")
                run_state = state.get("run", {})
                if run_state.get("finished") is True:
                    final_status = run_state.get("final_status")
                    if final_status not in ("completed", "success", "succeeded"):
                        raise MetaInferError(
                            f"MetaInfer task {remote_id} ended with {final_status}"
                        )
                    evidence = {"state": state}
                    for name in ("iterations", "state-graph"):
                        path = f"/api/{task_type}/{remote_id}/{name}"
                        response = await client.get(self.url + path, timeout=15)
                        if response.status_code == 404:
                            continue  # Optional plugin evidence endpoint.
                        response.raise_for_status()
                        evidence[name] = response.json()
                        artifacts[name] = self.url + path
                    return MetaInferResult(remote_id, "completed", evidence, artifacts)
                process = state.get("status", {})
                if process.get("running") is False and process.get("finished_at") is not None:
                    raise MetaInferError(
                        f"MetaInfer task {remote_id} stopped without terminal evidence"
                    )
                await asyncio.sleep(self.poll_interval)

        try:
            return await asyncio.wait_for(run(), timeout=self.timeout_seconds)
        except BaseException as exc:
            if remote_id:
                try:
                    stopped = await asyncio.wait_for(
                        self._request(
                            client,
                            "POST",
                            f"/api/sys-shell/{remote_id}/control",
                            json={"action": "kill", "force": True},
                        ),
                        timeout=5,
                    )
                    if not isinstance(stopped, dict) or stopped.get("ok") is not True:
                        raise MetaInferError("Service did not confirm kill")
                except (Exception, asyncio.CancelledError) as stop_exc:
                    raise MetaInferError(
                        f"MetaInfer task {remote_id}: termination unconfirmed; "
                        "stop it at the service"
                    ) from stop_exc
            if isinstance(exc, asyncio.TimeoutError):
                raise MetaInferError(f"MetaInfer deadline exceeded (task {remote_id})") from exc
            raise

    async def port_model(self, **kwargs: Any) -> MetaInferResult:
        return await self.execute(MetaInferTask(InferenceTaskType.PORT_MODEL, **kwargs))

    async def optimize_kernel(self, **kwargs: Any) -> MetaInferResult:
        return await self.execute(MetaInferTask(InferenceTaskType.OPTIMIZE_KERNEL, **kwargs))

    async def optimize_runtime(self, **kwargs: Any) -> MetaInferResult:
        return await self.execute(MetaInferTask(InferenceTaskType.OPTIMIZE_RUNTIME, **kwargs))

    async def analyze_trace(self, **kwargs: Any) -> MetaInferResult:
        return await self.execute(MetaInferTask(InferenceTaskType.ANALYZE_TRACE, **kwargs))

    async def benchmark(self, spec: Any, repository: str):
        from .benchmark import BenchmarkRunner

        runner = BenchmarkRunner(spec)
        runner.protect(repository)
        return await runner.measure(repository)
