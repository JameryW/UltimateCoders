"""Fault scenarios through UC's durable runtime and inference interfaces."""

import asyncio
import hashlib
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.inference import MetaInferResult, MetaInferTask, Oracle
from ultimate_coders.inference.adapter import MetaInferAdapter, RemoteStateUncertainError
from ultimate_coders.inference.benchmark import BenchmarkRunner, BenchmarkSpec
from ultimate_coders.inference.workflow import OptimizationWorkflow
from ultimate_coders.nats_worker import NatsWorker
from ultimate_coders.runtime_state import RuntimeState


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_finishes", [False, True])
async def test_duplicate_registration_keeps_original_execution_cancellable(duplicate_finishes):
    entered, capacity = asyncio.Event(), asyncio.Event()
    calls, started = [], []

    async def execute(*args, **kwargs):
        calls.append(None)
        if len(calls) == 2 and duplicate_finishes:
            return
        entered.set()
        await capacity.wait()
        started.append(None)

    worker = NatsWorker(mode="worker")
    worker._worker = SimpleNamespace(kill_node=lambda *_: False)
    worker._execute_and_report = execute
    subtask = Subtask(id="node", parent_id="graph")
    original = worker._spawn_subtask_execution(subtask)
    await entered.wait()
    duplicate = worker._spawn_subtask_execution(subtask)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert original in worker._running_node_tasks[("graph", "node")]
    assert worker._cancel_node_executions("graph", ["node"]) >= 1
    capacity.set()
    await asyncio.gather(original, duplicate, return_exceptions=True)
    assert original.cancelled()
    assert not started


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return RuntimeState()


@pytest.mark.asyncio
async def test_completed_job_recovers_concurrency_slot_after_crash(state):
    state.mutate(
        "remote_jobs",
        "op",
        lambda _: {
            "result": MetaInferResult("job", "completed").to_dict(),
        },
    )
    state.mutate("backend_slots", "slot", lambda _: {"operation_id": "op"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Completed recovery must not contact service")
        )
    ) as client:
        result = await MetaInferAdapter(
            "http://service", client=client, state=state, operation_id="op"
        ).analyze_trace(repository="repo", objective="trace")
    assert result.task_id == "job"
    assert state.get("backend_slots", "slot") == {}


@pytest.mark.asyncio
async def test_simultaneous_dispatches_execute_once_and_publish_saved_outcome(state):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def execute(_):
        entered.set()
        await finish.wait()
        return SubtaskResult(subtask_id="node", success=True, summary="accepted")

    worker = NatsWorker(mode="worker")
    worker._worker = SimpleNamespace(worker_id="w", execute_subtask=AsyncMock(side_effect=execute))
    worker._publisher = SimpleNamespace(publish_terminal=AsyncMock(return_value=True))
    subtask = Subtask(id="node", parent_id="graph")
    first_msg, duplicate_msg = (SimpleNamespace(ack=AsyncMock(), nak=AsyncMock()) for _ in range(2))
    first = asyncio.create_task(worker._execute_and_report_body(subtask, first_msg))
    await asyncio.wait_for(entered.wait(), 5)
    await worker._execute_and_report_body(subtask, duplicate_msg)
    duplicate_msg.nak.assert_awaited_once()
    duplicate_msg.ack.assert_not_awaited()
    finish.set()
    await first
    worker._worker.execute_subtask.assert_awaited_once()
    stored = state.get("result_outbox", "graph:node:0")
    assert stored["event"]["data"]["success"]
    assert worker._publisher.publish_terminal.call_args.args == (stored["event"], stored["update"])


@pytest.mark.asyncio
async def test_coordinator_restart_recovers_current_gateway_attempt(state):
    from ultimate_coders.agent.orchestrator import Orchestrator
    from ultimate_coders.agent.types import Task

    task = Task(
        id="graph", description="Optimize", subtasks=[Subtask(id="node", parent_id="graph")]
    )
    planner = NatsWorker(mode="default")
    planner._orchestrator = Orchestrator()
    planner._publisher = SimpleNamespace(publish_update=AsyncMock(return_value=True))
    await planner._publish_task_snapshot(task)
    raw = task.to_dict()
    raw["created_at"] = "2026-10-02T01:02:03.123456789Z"
    raw["updated_at"] = "2026-10-02T01:02:03Z"
    raw["status"] = "InProgress"
    raw["subtasks"][0].update(
        status="Assigned",
        dispatch_mode="PreferRemote",
        dispatch_retry_count=1,
        agent_config_json="{}",
    )
    restarted = NatsWorker(mode="default")
    restarted._orchestrator = Orchestrator()
    restarted._publisher = SimpleNamespace(confirm_update=AsyncMock(return_value=False))
    restarted._nc = SimpleNamespace(
        request=AsyncMock(return_value=SimpleNamespace(data=json.dumps({"task": raw}).encode()))
    )
    payload = {
        "type": "subtask_completed",
        "task_id": "graph",
        "subtask_id": "node",
        "message_id": "outcome:graph:node:1",
        "data": {"attempt_id": 1, "summary": "done"},
    }
    msg = SimpleNamespace(
        data=json.dumps(payload).encode(),
        reply="inbox",
        respond=AsyncMock(),
        headers={"UC-Recipient": "coordinator"},
    )
    await restarted._handle_task_event(msg)
    msg.respond.assert_not_awaited()
    assert state.get("coordinator_receipts", payload["message_id"])["confirmed"] is False
    restarted._orchestrator = Orchestrator()  # Another crash before confirmation.
    restarted._publisher.confirm_update.return_value = True
    await restarted._handle_task_event(msg)
    msg.respond.assert_awaited_once()
    assert state.get("coordinator_receipts", payload["message_id"])["confirmed"] is True
    sent = restarted._publisher.confirm_update.call_args.args[0]
    assert sent["status"] == "Completed"
    assert sent["subtasks"][0]["attempt_id"] == 1
    assert state.get("coordinator_tasks", "graph")["description"] == "Optimize"


def test_gateway_recovery_preserves_python_plan_and_normalizes_chrono_dates():
    from ultimate_coders.agent.types import Task

    task = Task(id="graph", verify_command="pytest", subtasks=[
        Subtask(id="node", project_id="scope", user_request="retain constraints",
                required_capabilities=["kernel_optimization"])
    ])
    raw = {"id": "graph", "status": "InProgress", "subtasks": [
        {"id": "node", "status": "Completed", "result": {
            "completed_at": "2026-10-02T01:02:03.123456789Z", "modified_files": []}}
    ], "created_at": "2026-10-02T01:02:03Z", "updated_at": "2026-10-02T01:02:03.1Z"}
    recovered = NatsWorker._coordinator_task_from_gateway(raw, NatsWorker._coordinator_plan(task))
    assert recovered.verify_command == "pytest"
    assert recovered.subtasks[0].project_id == "scope"
    assert recovered.subtasks[0].user_request == "retain constraints"
    assert recovered.subtasks[0].required_capabilities == ["kernel_optimization"]
    assert recovered.created_at.isoformat() == "2026-10-02T01:02:03+00:00"
    assert recovered.updated_at.isoformat() == "2026-10-02T01:02:03.100000+00:00"
    assert recovered.subtasks[0].result.completed_at.isoformat().endswith(".123456+00:00")


@pytest.mark.asyncio
async def test_benchmark_hard_limits_cover_every_sample(tmp_path):
    (tmp_path / "bench.py").write_text(
        "import json; from pathlib import Path; p=Path('sample'); "
        "i=int(p.read_text()) if p.exists() else 0; p.write_text(str(i+1)); "
        "print(json.dumps({'workload_id':'w','correctness':True,'compile_success':True,"
        "'numerical_error':None if i==2 else 0,'metrics':{'tpot_ms':43,"
        "'peak_memory_gb':[7.99,7.99,8.01][i%3]}}))"
    )
    runner = BenchmarkRunner(
        BenchmarkSpec([sys.executable, "-B", "bench.py"], "w", protected_paths=["bench.py"])
    )
    runner.protect(str(tmp_path))
    measured = await runner.measure(str(tmp_path))
    from ultimate_coders.inference import OraclePolicy

    reasons = Oracle(OraclePolicy(max_memory_gb=8, max_numerical_error=0.01)).validate(measured)
    assert any("excessive peak_memory" in reason for reason in reasons)
    assert any("numerical_error" in reason for reason in reasons)


@pytest.mark.asyncio
async def test_lost_submission_response_never_creates_another_job(state):
    posts = []

    def service(request):
        if request.method == "GET":
            return httpx.Response(200, json={"fields": []})
        posts.append(request)
        raise httpx.ReadTimeout("response lost")

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        for _ in range(2):
            with pytest.raises(RemoteStateUncertainError) as error:
                await MetaInferAdapter(
                    "http://service", client=client, state=state, operation_id="graph:node:0"
                ).analyze_trace(repository="repo", objective="trace")
            assert error.value.cleanup_pending
            assert not error.value.retryable
    assert len(posts) == 1
    assert state.get("remote_jobs", "graph:node:0")["state"] == "submission_unknown"


@pytest.mark.asyncio
async def test_restart_adopts_saved_remote_id_without_submission(state):
    state.mutate(
        "remote_jobs",
        "op",
        lambda _: {
            "remote_id": "job-1",
            "state": "running",
            "deadline": time.time() + 30,
        },
    )
    seen = []

    def service(request):
        seen.append(request.method)
        if request.url.path.endswith("schema"):
            return httpx.Response(200, json={"fields": []})
        if request.url.path == "/api/sys-shell/job-1":
            return httpx.Response(
                200, json={"run": {"finished": True, "final_status": "completed"}}
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        result = await MetaInferAdapter(
            "http://service", client=client, state=state, operation_id="op"
        ).analyze_trace(repository="repo", objective="trace")
    assert result.task_id == "job-1"
    assert "POST" not in seen
    assert state.get("remote_jobs", "op")["state"] == "completed"


@pytest.mark.asyncio
async def test_result_outbox_survives_disconnect_and_worker_restart(state):
    execute = AsyncMock(
        return_value=SubtaskResult(subtask_id="node", summary="accepted", success=True)
    )
    worker = NatsWorker(mode="worker")
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=execute)
    worker._publisher = SimpleNamespace(publish_terminal=AsyncMock(return_value=False))
    message = SimpleNamespace(ack=AsyncMock())
    subtask = Subtask(id="node", parent_id="graph")
    await worker._execute_and_report_body(subtask, js_msg=message)
    message.ack.assert_awaited_once()
    assert state.get("result_outbox", "graph:node:0")["delivered"] is False

    restarted = NatsWorker(mode="worker")
    restarted._worker = SimpleNamespace(worker_id="w2", execute_subtask=execute)
    restarted._publisher = SimpleNamespace(publish_terminal=AsyncMock(return_value=True))
    await restarted._execute_and_report_body(subtask, js_msg=SimpleNamespace(ack=AsyncMock()))
    execute.assert_awaited_once()
    assert state.get("result_outbox", "graph:node:0")["delivered"] is True
    before = worker._publisher.publish_terminal.call_args.args
    after = restarted._publisher.publish_terminal.call_args.args
    assert before == after


@pytest.mark.asyncio
async def test_unconfirmed_writer_preserves_checkpoint_and_original_baseline(tmp_path):
    from tests.python.test_inference_workflow import _make_repository

    root = _make_repository(tmp_path)
    artifact_dir = str(tmp_path / "artifacts")
    task = MetaInferTask("optimize_kernel", str(root), "minimize TPOT")

    def benchmark():
        return BenchmarkRunner(
            BenchmarkSpec([sys.executable, "-B", "bench.py"], "fixed", protected_paths=["bench.py"])
        )

    async def uncertain(_):
        (root / "latency.txt").write_text("37")
        raise RemoteStateUncertainError("kill not confirmed")

    with pytest.raises(RemoteStateUncertainError):
        await OptimizationWorkflow(Oracle()).run(task, benchmark(), uncertain, artifact_dir)
    assert (root / "latency.txt").read_text() == "37"

    async def adopted(_):
        return MetaInferResult("existing-job", "completed")

    result = await OptimizationWorkflow(Oracle()).run(task, benchmark(), adopted, artifact_dir)
    assert result["success"]
    assert result["baseline"]["metrics"]["tpot_ms"] == 43
    assert result["optimized"]["metrics"]["tpot_ms"] == 37


@pytest.mark.asyncio
async def test_noisy_benchmark_is_rejected(tmp_path):
    (tmp_path / "bench.py").write_text(
        "import json; from pathlib import Path; p=Path('sample'); "
        "i=int(p.read_text()) if p.exists() else 0; p.write_text(str(i+1)); "
        "print(json.dumps({'workload_id':'w','correctness':True,'compile_success':True,"
        "'metrics':{'tpot_ms':[43,10,100,40][i%4]}}))"
    )
    runner = BenchmarkRunner(
        BenchmarkSpec([sys.executable, "-B", "bench.py"], "w", protected_paths=["bench.py"])
    )
    runner.protect(str(tmp_path))
    measured = await runner.measure(str(tmp_path))
    assert measured.statistics["count"] == 3
    assert measured.environment_id
    assert any("Unstable" in reason for reason in Oracle().validate(measured))


@pytest.mark.asyncio
async def test_dashboard_artifact_authentication_and_integrity(state, tmp_path, monkeypatch):
    from ultimate_coders.dashboard.app import DashboardApp

    root = tmp_path / "artifacts"
    experiment = root / "experiment"
    experiment.mkdir(parents=True)
    report = experiment / "report.json"
    report.write_text('{"success": true}')
    monkeypatch.setenv("UC_INFERENCE_ARTIFACT_DIR", str(root))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "fixture-password")
    monkeypatch.setenv("UC_METRICS_DB", str(tmp_path / "metrics.db"))
    monkeypatch.setattr(
        "ultimate_coders.dashboard.metrics._ALERTS_DB_PATH", str(tmp_path / "metrics.db")
    )
    state.mutate(
        "experiments",
        "experiment",
        lambda _: {
            "artifact_dir": str(experiment),
            "identity": {"graph_id": "graph"},
            "artifacts": {
                "report.json": {"sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
            },
        },
    )
    dashboard = DashboardApp(orchestrator=None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard._app, client=("203.0.113.1", 123)),
            base_url="http://test",
        ) as client:
            assert (await client.get("/dashboard/api/experiments")).status_code == 401
            client.headers["Authorization"] = "Bearer fixture-password"
            assert (
                len(
                    (await client.get("/dashboard/api/experiments?task_id=graph")).json()[
                        "experiments"
                    ]
                )
                == 1
            )
            url = "/dashboard/api/experiments/experiment/artifacts/report.json"
            assert (await client.get(url)).status_code == 200
            report.write_text("tampered")
            assert (await client.get(url)).status_code == 409
            assert (
                await client.get(url.replace("report.json", "checkpoint.json"))
            ).status_code == 404
    finally:
        dashboard._metrics.close()
