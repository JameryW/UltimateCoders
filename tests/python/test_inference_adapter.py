"""HTTP fixtures match MetaInfer's pinned sys-shell protocol."""

import asyncio
import json

import httpx
import pytest
from ultimate_coders.inference import MetaInferTask
from ultimate_coders.inference.adapter import MetaInferAdapter, MetaInferError


@pytest.mark.asyncio
async def test_port_model_submits_live_schema_and_keeps_artifacts(tmp_path):
    seen = []

    def service(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/schema"):
            return httpx.Response(
                200,
                json={
                    "fields": [
                        {"key": "model_params_path", "required": True},
                        {"key": "target_framework_dir", "required": True},
                        {"key": "worker_nodes", "required": False},
                    ]
                },
            )
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["type"] == "port-model"
            assert body["answers"]["worker_nodes"] == "gpu-a,gpu-b"
            assert "minimize TPOT" in body["raw_request"]
            return httpx.Response(200, json={"task_id": "port-123", "workspace_dir": "/nfs/p1"})
        if request.url.path.endswith("/port-123"):
            return httpx.Response(
                200,
                json={
                    "type": "port-model",
                    "status": {"running": False},
                    "run": {"finished": True, "final_status": "completed"},
                },
            )
        return httpx.Response(200, json=[] if request.url.path.endswith("iterations") else {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        adapter = MetaInferAdapter("http://metainfer:8765", client=client)
        result = await adapter.port_model(
            repository=str(tmp_path),
            objective="minimize TPOT",
            framework="sglang",
            parameters={
                "model_params_path": "/weights/qwen",
                "target_framework_dir": str(tmp_path),
                "worker_nodes": "gpu-a,gpu-b",
            },
        )
    assert result.status == "completed"
    assert result.artifacts["workspace_dir"] == "/nfs/p1"
    assert "iterations" in result.evidence
    assert seen[0][1] == "/api/sys-shell/task-types/port-model/schema"


@pytest.mark.asyncio
async def test_missing_parameter_never_starts_a_gpu_job():
    requests = []

    def service(request):
        requests.append(request)
        return httpx.Response(200, json={"fields": [{"key": "kernel_file_path", "required": True}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        with pytest.raises(ValueError, match="kernel_file_path"):
            await MetaInferAdapter("http://service", client=client).optimize_kernel(
                repository="repo",
                objective="faster",
            )
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deadline", "cancel", "failed", "kill_unconfirmed"])
async def test_failure_and_cancellation_terminate_remote_jobs(mode):
    started = asyncio.Event()
    killed = []

    async def service(request):
        if request.url.path.endswith("schema"):
            return httpx.Response(200, json={"fields": []})
        if request.url.path.endswith("/control"):
            killed.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": mode != "kill_unconfirmed"})
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "job-1"})
        started.set()
        if mode == "failed":
            return httpx.Response(200, json={"run": {"finished": True, "final_status": "failed"}})
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        adapter = MetaInferAdapter(
            "http://service", client=client, timeout_seconds=0.02 if mode != "cancel" else 30
        )
        future = asyncio.create_task(
            adapter.execute(MetaInferTask("analyze_trace", "repo", "trace"))
        )
        await started.wait()
        if mode == "cancel":
            future.cancel()
        expected = asyncio.CancelledError if mode == "cancel" else MetaInferError
        with pytest.raises(expected):
            await future
    assert killed == [{"action": "kill", "force": True}]


@pytest.mark.asyncio
async def test_submission_failure_is_not_retried():
    posts = []

    def service(request):
        if request.method == "GET":
            return httpx.Response(200, json={"fields": []})
        posts.append(request)
        return httpx.Response(500, json={"error": "spawn failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        with pytest.raises(MetaInferError):
            await MetaInferAdapter("http://service", client=client).analyze_trace(
                repository="repo",
                objective="trace",
            )
    assert len(posts) == 1
