"""Execution-only client for MetaInfer's real task/form HTTP protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from ultimate_coders.runtime_state import RuntimeState

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


def task_types() -> dict:
    overrides = json.loads(os.environ.get("UC_METAINFER_TASK_TYPES") or "{}")
    if not isinstance(overrides, dict):
        raise ValueError("UC_METAINFER_TASK_TYPES must be an object")
    result = {
        operation: overrides.get(operation.value, plugin)
        for operation, plugin in DEFAULT_TASK_TYPES.items()
    }
    if any(
        not isinstance(plugin, str) or not _SAFE_ID.fullmatch(plugin) for plugin in result.values()
    ):
        raise ValueError("Invalid MetaInfer plugin identifier")
    return result


class MetaInferError(RuntimeError):
    """Service failure; never an implicit fallback to a generic coding agent."""


class RemoteStateUncertainError(MetaInferError):
    """Remote code may still be executing; preserve workspace and prohibit retries."""

    retryable = False
    cleanup_pending = True


class MetaInferAdapter:
    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 3600,
        poll_interval: float = 2,
        state: RuntimeState | None = None,
        operation_id: str | None = None,
        max_concurrency: int = 1,
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
        self.state, self.operation_id = state, operation_id
        self.max_concurrency = max_concurrency
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")

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
        task_type = task.upstream_type or task_types()[task.task_type]
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
        key = self.operation_id
        request_sha = hashlib.sha256(
            json.dumps(task.to_dict(), sort_keys=True).encode()
        ).hexdigest()
        record = (
            await asyncio.to_thread(self.state.get, "remote_jobs", key)
            if self.state and key
            else None
        )
        if record:
            if (
                record.get("backend", self.url) != self.url
                or record.get("task_type", task_type) != task_type
                or record.get("request_sha256", request_sha) != request_sha
            ):
                raise RemoteStateUncertainError(
                    "Remote operation identity changed; reconcile before retry"
                )
            if record.get("result"):
                # Completion is persisted before slot release. Recover that
                # crash window instead of leaking a backend's entire budget.
                if self.state:
                    for slot in await asyncio.to_thread(self.state.records, "backend_slots"):
                        if slot.get("operation_id") == key:
                            await asyncio.to_thread(
                                self.state.mutate,
                                "backend_slots",
                                slot["key"],
                                lambda old: {} if old.get("operation_id") == key else old,
                            )
                return MetaInferResult(**record["result"])
            remote_id = record.get("remote_id")
            if not remote_id:
                raise RemoteStateUncertainError(
                    "Submission outcome unknown; reconcile at MetaInfer before retry"
                )
        backend_key = hashlib.sha256(self.url.encode()).hexdigest()
        slot_key = None

        async def persist(**values):
            if self.state and key:
                return await asyncio.to_thread(
                    self.state.mutate,
                    "remote_jobs",
                    key,
                    lambda old: {**old, **values},
                )

        async def reserve() -> None:
            nonlocal slot_key
            if not self.state or not key:
                return
            for index in range(self.max_concurrency):
                slot = f"{backend_key}:{index}"
                value = await asyncio.to_thread(
                    self.state.mutate,
                    "backend_slots",
                    slot,
                    lambda old: (
                        {"operation_id": key}
                        if not old.get("operation_id") or old.get("operation_id") == key
                        else old
                    ),
                )
                if value.get("operation_id") == key:
                    slot_key = slot
                    return
            raise MetaInferError("MetaInfer concurrency budget exhausted")

        async def release_slot() -> None:
            if slot_key:
                await asyncio.to_thread(
                    self.state.mutate,
                    "backend_slots",
                    slot_key,
                    lambda old: {} if old.get("operation_id") == key else old,
                )

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
            for field_key, spec in fields.items():
                if field_key not in answers and spec.get("default") is not None:
                    answers[field_key] = spec["default"]
                if spec.get("required") and answers.get(field_key) in (None, "", []):
                    raise ValueError(f"{task_type} requires parameter {field_key}")
            payload = {
                "type": task_type,
                "label": "UC " + (key or task.task_type.value),
                "raw_request": json.dumps(task.to_dict(), ensure_ascii=False),
                "answers": answers,
            }
            artifacts = dict((record or {}).get("artifacts", {}))
            if not remote_id:
                await reserve()
                if self.state and key:
                    claim = uuid.uuid4().hex
                    claimed = await asyncio.to_thread(
                        self.state.mutate,
                        "remote_jobs",
                        key,
                        lambda old: (
                            old
                            or {
                                "state": "submission_unknown",
                                "operation_id": key,
                                "claim": claim,
                                "task_type": task_type,
                                "backend": self.url,
                                "request_sha256": request_sha,
                                "payload_sha256": hashlib.sha256(
                                    json.dumps(payload, sort_keys=True).encode()
                                ).hexdigest(),
                                "deadline": time.time() + self.timeout_seconds,
                            }
                        ),
                    )
                    # A racing process already owns this intent. Never repeat POST.
                    if claimed.get("claim") != claim:
                        raise RemoteStateUncertainError(
                            "Concurrent remote operation; attach through its owner"
                        )
                try:
                    created = await self._request(
                        client,
                        "POST",
                        f"{shell}/tasks",
                        json=payload,
                    )
                    remote_id = created.get("task_id")
                    if not isinstance(remote_id, str) or not _SAFE_ID.fullmatch(remote_id):
                        remote_id = None
                        raise MetaInferError("MetaInfer returned an invalid task_id")
                    artifacts = {
                        key: created[key]
                        for key in ("workspace_dir", "state_dir")
                        if isinstance(created.get(key), str)
                    }
                    await persist(state="running", remote_id=remote_id, artifacts=artifacts)
                except BaseException as exc:
                    if self.state and key:
                        raise RemoteStateUncertainError(
                            "MetaInfer submission outcome unknown; workspace quarantined"
                        ) from exc
                    raise
            else:
                await reserve()
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
                    result = MetaInferResult(remote_id, "completed", evidence, artifacts)
                    await persist(state="completed", result=result.to_dict())
                    await release_slot()
                    return result
                process = state.get("status", {})
                if process.get("running") is False and process.get("finished_at") is not None:
                    raise MetaInferError(
                        f"MetaInfer task {remote_id} stopped without terminal evidence"
                    )
                await asyncio.sleep(self.poll_interval)

        try:
            remaining = max(
                0.01,
                (record or {}).get("deadline", time.time() + self.timeout_seconds) - time.time(),
            )
            return await asyncio.wait_for(run(), timeout=remaining)
        except BaseException as exc:
            if isinstance(exc, RemoteStateUncertainError):
                raise
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
                    await persist(state="cleanup_pending", remote_id=remote_id)
                    raise RemoteStateUncertainError(
                        f"MetaInfer task {remote_id}: termination unconfirmed; "
                        "stop it at the service"
                    ) from stop_exc
                await persist(state="stopped", remote_id=remote_id)
                await release_slot()
            elif (
                not self.state
                or not key
                or not await asyncio.to_thread(self.state.get, "remote_jobs", key)
            ):
                await release_slot()
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
