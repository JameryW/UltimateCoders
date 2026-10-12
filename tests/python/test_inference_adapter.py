"""HTTP fixtures match MetaInfer's pinned sys-shell protocol."""

import asyncio
import hashlib
import json

import httpx
import pytest
from ultimate_coders.inference import MetaInferTask
from ultimate_coders.inference.adapter import MetaInferAdapter, MetaInferError

from .metainfer_fixture import contract_response


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "modified", "missing_receipt", "missing_stop"])
async def test_cached_kernel_completion_revalidates_stopped_repository_without_resubmission(
    tmp_path, change
):
    from ultimate_coders.inference.adapter import RemoteStateUncertainError
    from ultimate_coders.inference.resources import ResourceBudget
    from ultimate_coders.inference.service_contract import DeploymentContractError
    from ultimate_coders.runtime_state import RuntimeState

    kernel = tmp_path / "kernel.py"
    kernel.write_text("accepted candidate\n")
    task = MetaInferTask(
        "optimize_kernel",
        str(tmp_path),
        "faster",
        constraints={"repository_candidate_required": True},
        parameters={"kernel_file_path": str(kernel)},
    )
    receipt = {
        "kernel_path": "kernel.py",
        "candidate_id": "winner",
        "code_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
        "publication_kind": "oci_generated_candidate",
        "oracle_acceptance": "pending",
    }
    run = {"finished": True, "final_status": "success", "uc_candidate": receipt}
    proof = contract_response("/api/uc/jobs/kernel-1/quiescence")
    if change == "modified":
        kernel.write_text("rolled back or modified\n")
    if change == "missing_receipt":
        run.pop("uc_candidate")
    if change == "missing_stop":
        proof = {}
    state = RuntimeState(tmp_path / "runtime.db", database_url="")
    budget = ResourceBudget(state, "fixture-backend")
    await budget.acquire("op", timeout=1)
    state.mutate("backend_slots", "legacy-slot", lambda _: {"operation_id": "op"})
    state.mutate(
        "remote_jobs",
        "op",
        lambda _: {
            "state": "completed",
            "backend_id": "fixture-backend",
            "task_type": "evolve-kernel",
            "request_sha256": hashlib.sha256(
                json.dumps(task.to_dict(), sort_keys=True).encode()
            ).hexdigest(),
            "result": {
                "task_id": "kernel-1",
                "status": "completed",
                "evidence": {"state": {"run": run}, "quiescence": proof},
            },
        },
    )
    seen = []

    def service(request):
        seen.append(request.url.path)
        body = json.loads(request.content) if request.content else None
        response = contract_response(request.url.path, body)
        if request.url.path == "/api/uc/contract":
            response["capabilities"].append("kernel_publication")
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        execution = MetaInferAdapter(
            "http://service", client=client, state=state, operation_id="op"
        ).execute(task)
        if change == "unchanged":
            assert (await execution).status == "completed"
        else:
            error = (
                RemoteStateUncertainError if change == "missing_stop" else DeploymentContractError
            )
            with pytest.raises(error):
                await execution
    assert "/api/sys-shell/tasks" not in seen
    holders = state.get("resource_budgets", "fixture-backend")["holders"]
    assert bool(holders) is (change == "missing_stop")
    assert state.get("backend_slots", "legacy-slot") == (
        {"operation_id": "op"} if change == "missing_stop" else {}
    )


@pytest.mark.asyncio
async def test_workflow_kernel_requires_publication_capability_before_submission(tmp_path):
    from ultimate_coders.inference.service_contract import DeploymentContractError

    seen = []

    def service(request):
        seen.append(request.url.path)
        body = json.loads(request.content) if request.content else None
        return httpx.Response(200, json=contract_response(request.url.path, body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        with pytest.raises(DeploymentContractError, match="kernel_publication"):
            await MetaInferAdapter(
                "http://service", client=client, timeout_seconds=0.2
            ).optimize_kernel(
                repository=str(tmp_path),
                objective="faster",
                constraints={"repository_candidate_required": True},
            )
    assert "/api/sys-shell/tasks" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["published", "missing", "failed_after_stop", "wrong_checksum"])
async def test_kernel_completion_observes_publication_after_quiescence(tmp_path, outcome):
    from ultimate_coders.inference.service_contract import DeploymentContractError

    sequence = []
    kernel = tmp_path / "kernel.py"
    kernel.write_text("candidate code\n")

    def service(request):
        path = request.url.path
        sequence.append(path)
        body = json.loads(request.content) if request.content else None
        contract = contract_response(path, body)
        if contract is not None:
            if path == "/api/uc/contract":
                contract["capabilities"].append("kernel_publication")
            return httpx.Response(200, json=contract)
        if path.endswith("/schema"):
            return httpx.Response(
                200, json={"fields": [{"key": "kernel_file_path", "required": True}]}
            )
        if path.endswith("/control"):
            return httpx.Response(200, json={"ok": True})
        if path == "/api/sys-shell/tasks":
            return httpx.Response(200, json={"task_id": "kernel-1"})
        if path == "/api/sys-shell/kernel-1":
            stopped = any(p.endswith("/quiescence") for p in sequence)
            run = {"finished": True, "final_status": "success"}
            if stopped and outcome == "failed_after_stop":
                run["final_status"] = "failed"
            if stopped and outcome in ("published", "wrong_checksum"):
                run["uc_candidate"] = {
                    "kernel_path": "kernel.py",
                    "candidate_id": "winner",
                    "code_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest()
                    if outcome == "published"
                    else "a" * 64,
                    "publication_kind": "oci_generated_candidate",
                    "oracle_acceptance": "pending",
                }
            return httpx.Response(200, json={"run": run})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        execution = MetaInferAdapter("http://service", client=client).optimize_kernel(
            repository=str(tmp_path),
            objective="faster",
            constraints={"repository_candidate_required": True},
            parameters={"kernel_file_path": str(kernel)},
        )
        if outcome == "published":
            result = await execution
            assert result.evidence["state"]["run"]["uc_candidate"]["oracle_acceptance"] == "pending"
        else:
            with pytest.raises(DeploymentContractError, match="publication evidence"):
                await execution
    assert sequence.count("/api/sys-shell/kernel-1") == 2


@pytest.mark.asyncio
async def test_port_model_submits_live_schema_and_keeps_artifacts(tmp_path):
    seen = []

    def service(request):
        seen.append((request.method, request.url.path))
        contract = contract_response(
            request.url.path, json.loads(request.content) if request.content else None
        )
        if contract is not None:
            return httpx.Response(200, json=contract)
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
    assert ("GET", "/api/sys-shell/task-types/port-model/schema") in seen


@pytest.mark.asyncio
async def test_missing_parameter_never_starts_a_gpu_job(tmp_path):
    requests = []

    def service(request):
        requests.append(request)
        contract = contract_response(
            request.url.path, json.loads(request.content) if request.content else None
        )
        if contract is not None:
            return httpx.Response(200, json=contract)
        return httpx.Response(200, json={"fields": [{"key": "kernel_file_path", "required": True}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        with pytest.raises(ValueError, match="kernel_file_path"):
            await MetaInferAdapter("http://service", client=client).optimize_kernel(
                repository=str(tmp_path),
                objective="faster",
            )
    assert not any(request.url.path == "/api/sys-shell/tasks" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("widget", "value", "valid"),
    [
        ("select", "1", False),
        ("select", 10, False),
        ("select", ["10"], False),
        ("select", "10", True),
        ("select", None, True),
        ("multiselect", "10", False),
        ("multiselect", ["1"], False),
        ("multiselect", ["10", "20"], True),
        ("multiselect", [], True),
    ],
)
async def test_form_choices_validate_before_submission_intent(tmp_path, widget, value, valid):
    from ultimate_coders.runtime_state import RuntimeState

    state = RuntimeState(tmp_path / "state.db", database_url="")
    submitted = []

    def service(request):
        if request.url.path.endswith("/schema"):
            return httpx.Response(
                200,
                json={"fields": [{"key": "choice", "type": widget,
                                  "options": [{"label": "10"}, {"label": "20"}]}]},
            )
        if request.method == "POST":
            submitted.append(json.loads(request.content)["answers"])
            return httpx.Response(200, json={"task_id": "job-1"})
        if request.url.path.endswith("/job-1"):
            return httpx.Response(200, json={"run": {"finished": True, "final_status": "success"}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        execution = MetaInferAdapter(
            "http://service", client=client, state=state, operation_id="choices"
        ).analyze_trace(repository="repo", objective="trace", parameters={"choice": value})
        if valid:
            assert (await execution).status == "completed"
            assert submitted == [{"choice": value}]
        else:
            with pytest.raises(ValueError, match="choice"):
                await execution
            assert submitted == []
            assert state.get("remote_jobs", "choices") is None
    assert not any(record.get("holders") for record in state.records("resource_budgets"))
    assert not any(record.get("operation_id") for record in state.records("backend_slots"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deadline", "cancel", "failed", "kill_unconfirmed"])
async def test_failure_and_cancellation_terminate_remote_jobs(mode):
    started = asyncio.Event()
    killed = []

    async def service(request):
        if request.url.path.endswith("/quiescence"):
            import hashlib

            return httpx.Response(
                200,
                json=contract_response(
                    request.url.path, backend_id=hashlib.sha256(b"http://service").hexdigest()
                ),
            )
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
