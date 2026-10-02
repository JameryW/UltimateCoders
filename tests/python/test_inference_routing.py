"""Domain routing, configuration forwarding and UC execution boundaries."""

import json
import sys
from unittest.mock import AsyncMock

import pytest
from ultimate_coders.agent.harness_metainfer import MetaInferAgentAdapter
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.sandbox import AgentOutput, ExecResult, SandboxConfig, SandboxManager
from ultimate_coders.agent.types import Subtask, WorkflowStep
from ultimate_coders.agent.worker import Worker
from ultimate_coders.inference import InferenceInfraAgent
from ultimate_coders.inference.runner import run_inference
from ultimate_coders.nats_worker import NatsPublisher


@pytest.mark.parametrize(
    "text,expected",
    [
        ("SGLang FP8 TP8 OOM", "optimize_runtime"),
        ("optimize this CUDA kernel", "optimize_kernel"),
        ("移植 vLLM 模型到 A800", "port_model"),
        ("analyze SGLang trace", "analyze_trace"),
        ("benchmark vLLM", "benchmark"),
        ("fix the React login button", None),
        ("Linux kernel panic", None),
        ("add documentation for SGLang", None),
    ],
)
def test_routing_needs_domain_and_intent(monkeypatch, text, expected):
    monkeypatch.setenv("UC_METAINFER_URL", "http://service")
    monkeypatch.delenv("UC_INFERENCE_TASK_JSON", raising=False)
    route = InferenceInfraAgent.route(text)
    assert (route["inference_task"]["task_type"] if route else None) == expected


def test_routing_and_capabilities_are_optional_and_explicit_agent_wins(monkeypatch):
    monkeypatch.delenv("UC_METAINFER_URL", raising=False)
    assert InferenceInfraAgent.route("optimize SGLang") is None
    assert "inference_infra" not in Worker().capabilities
    monkeypatch.setenv("UC_METAINFER_URL", "http://service")
    assert "inference_infra" not in Worker().capabilities  # URL alone is not readiness.
    assert "inference_benchmark" in Worker().capabilities
    assert InferenceInfraAgent.route("optimize SGLang", {"agent": "codex"}) is None


@pytest.mark.asyncio
async def test_explicit_task_is_one_domain_node_and_skips_global_replanning():
    llm = AsyncMock()
    orch = Orchestrator(llm_client=llm)
    config = {
        "inference_task": {
            "task_type": "port_model",
            "objective": "port qwen",
            "parameters": {"model_params_path": "/weights"},
        }
    }
    task = await orch.submit_task("Port the model\nMeasure results", agent_config=config)
    assert len(task.subtasks) == 1
    assert task.subtasks[0].agent_config["agent"] == "metainfer"
    assert task.subtasks[0].required_capabilities == ["inference_infra", "model_porting"]
    llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_publisher_preserves_explicit_domain_configuration():
    nats = AsyncMock()
    config = {"inference_task": {"task_type": "analyze_trace"}}
    await NatsPublisher(nats).publish_submit("t1", "analyze", agent_config=config)
    subject, payload = nats.publish.call_args.args
    assert subject == "uc.task.submit"
    assert json.loads(payload)["agent_config"] == config


@pytest.mark.asyncio
async def test_worker_selects_domain_adapter_and_records_only_accepted_evidence():
    worker = Worker()
    worker._sandbox_manager.execute = AsyncMock(
        return_value=AgentOutput(
            success=True,
            summary="accepted",
            domain_result={"graph": {"version": 1}},
        )
    )
    worker.write_shared_memory = AsyncMock()
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        project_id="p1",
        description="port model",
        agent_config={"inference_task": {"task_type": "port_model"}},
    )
    result = await worker._execute_in_sandbox(subtask)
    assert result.success
    assert worker._sandbox_manager.execute.call_args.kwargs["agent"] == "metainfer"
    assert worker.write_shared_memory.call_args.kwargs["project_id"] == "p1"
    worker.write_shared_memory.reset_mock()
    worker._sandbox_manager.execute.return_value.success = False
    await worker._execute_in_sandbox(subtask)
    worker.write_shared_memory.assert_not_called()


@pytest.mark.asyncio
async def test_local_benchmark_runner_produces_persistent_report(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bench.py").write_text(
        "import json\nprint(json.dumps({'workload_id':'fixed', 'correctness':True, "
        "'compile_success':True,'metrics':{'tpot_ms':43}}))",
        encoding="utf-8",
    )
    monkeypatch.setenv("UC_INFERENCE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    result = await run_inference(
        {
            "cwd": str(root),
            "config": {
                "inference_task": {"task_type": "benchmark", "objective": "measure"},
                "benchmark": {
                    "command": [sys.executable, "-B", "bench.py"],
                    "workload_id": "fixed",
                    "protected_paths": ["bench.py"],
                },
            },
        }
    )
    assert result["success"]
    assert result["measurement"]["metrics"]["tpot_ms"] == 43
    from pathlib import Path

    assert Path(result["artifacts"]["report"]).is_file()


def test_adapter_requires_a_valid_final_envelope():
    adapter = MetaInferAgentAdapter()
    assert not adapter.parse_output(ExecResult(exit_code=0, stdout="completed")).success
    assert not adapter.parse_output(
        ExecResult(
            exit_code=1,
            stdout=json.dumps(
                {"event": "final", "success": True, "summary": "accepted"},
            ),
        )
    ).success
    output = adapter.parse_output(
        ExecResult(
            exit_code=0,
            stdout=json.dumps(
                {"event": "final", "success": True, "summary": "accepted", "result": {"graph": {}}},
            ),
        )
    )
    assert output.success
    assert output.domain_result == {"graph": {}}

    output = adapter.parse_output(
        ExecResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "event": "final",
                    "success": True,
                    "result": {
                        "file_changes": [
                            {"file_path": "kernels/new.py", "change_type": "created"},
                        ]
                    },
                }
            ),
        )
    )
    assert output.file_changes[0].file_path == "kernels/new.py"
    assert output.file_changes[0].change_type.value == "created"


@pytest.mark.asyncio
async def test_workflow_retains_accepted_domain_evidence_before_memory_publication():
    worker = Worker()
    worker._sandbox_manager.execute = AsyncMock(
        side_effect=[
            AgentOutput(success=True, summary="accepted", domain_result={"graph": {"version": 1}}),
            AgentOutput(success=True, summary="reviewed"),
        ]
    )
    worker.write_shared_memory = AsyncMock()
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        project_id="p1",
        description="optimize",
        steps=[
            WorkflowStep(
                agent="metainfer",
                prompt="optimize",
                agent_config={"inference_task": {"task_type": "optimize_kernel"}},
            ),
            WorkflowStep(agent="codex", prompt="review"),
        ],
    )
    assert (await worker._execute_in_sandbox(subtask)).success
    evidence = json.loads(worker.write_shared_memory.call_args.kwargs["content"])
    assert evidence["steps"][0] == {
        "index": 0,
        "agent": "metainfer",
        "result": {"graph": {"version": 1}},
    }


@pytest.mark.asyncio
async def test_sandbox_executes_the_installed_domain_runner(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bench.py").write_text(
        "import json\nprint(json.dumps({'workload_id':'fixed','correctness':True,"
        "'compile_success':True,'metrics':{'throughput_tokens_s':125}}))",
        encoding="utf-8",
    )
    from pathlib import Path

    python_source = str(Path(__file__).resolve().parents[2] / "python")
    manager = SandboxManager(
        SandboxConfig(
            agent="metainfer",
            project_path=str(root),
            env_vars={
                "PYTHONPATH": python_source,
                "UC_INFERENCE_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            },
        )
    )
    output = await manager.execute(
        "Benchmark throughput",
        subtask_config={
            "inference_task": {"task_type": "benchmark", "objective": "measure throughput"},
            "benchmark": {
                "command": [sys.executable, "-B", "bench.py"],
                "workload_id": "fixed",
                "protected_paths": ["bench.py"],
            },
        },
    )
    assert output.success, output.summary
    assert output.domain_result["measurement"]["metrics"]["throughput_tokens_s"] == 125


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_via", ["coroutine", "control"])
async def test_sandbox_cancellation_stops_remote_job_and_rolls_back(tmp_path, cancel_via):
    import asyncio
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from types import SimpleNamespace

    root = tmp_path / "repo"
    root.mkdir()
    (root / "latency.txt").write_text("43")
    (root / "bench.py").write_text(
        "import json; from pathlib import Path; print(json.dumps({'workload_id':'fixed',"
        "'correctness':True,'compile_success':True,'metrics':"
        "{'tpot_ms':float(Path('latency.txt').read_text())}}))"
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    started, stopped = threading.Event(), threading.Event()

    class Service(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload):
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path.endswith("/schema"):
                self.reply({"fields": [{"key": "kernel_file_path", "required": True}]})
            else:
                started.set()  # Polling means the runner already knows the remote task ID.
                self.reply({"run": {"finished": False}, "status": {"running": True}})

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/control"):
                stopped.set()
                self.reply({"ok": True})
            else:
                (root / "latency.txt").write_text("10")
                self.reply({"task_id": "job-1", "workspace_dir": str(root)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = SimpleNamespace(execute_in_sandbox=AsyncMock())
    manager = SandboxManager(
        SandboxConfig(
            agent="metainfer",
            project_path=str(root),
            max_cpu_seconds=30,
            env_vars={
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "python"),
                "UC_METAINFER_URL": f"http://127.0.0.1:{server.server_port}",
                "UC_INFERENCE_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            },
        ),
        engine=engine,
    )
    key = ("task", "node")
    running = asyncio.create_task(
        manager.execute(
            "optimize kernel",
            cancel_key=key,
            subtask_config={
                "inference_task": {
                    "task_type": "optimize_kernel",
                    "objective": "minimize TPOT",
                    "parameters": {"kernel_file_path": "latency.txt"},
                },
                "benchmark": {
                    "command": [sys.executable, "-B", "bench.py"],
                    "workload_id": "fixed",
                    "protected_paths": ["bench.py"],
                },
            },
        )
    )

    async def wait_until_started():
        while not started.is_set():
            if running.done():
                pytest.fail((await running).summary)
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(wait_until_started(), 8)
        if cancel_via == "coroutine":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 10)
        else:
            assert manager.kill_group(key)
            assert not (await asyncio.wait_for(running, 10)).success
        assert stopped.is_set()
        assert (root / "latency.txt").read_text() == "43"
        assert key not in manager._active_procs
        engine.execute_in_sandbox.assert_not_called()
        report = next((tmp_path / "artifacts").glob("*/report.json"))
        assert json.loads(report.read_text())["success"] is False
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_dashboard_submit_forwards_domain_config_and_rejects_wrong_shape(
    tmp_path, monkeypatch
):
    import httpx
    from ultimate_coders.dashboard.app import DashboardApp

    monkeypatch.setenv("UC_METRICS_DB", str(tmp_path / "metrics.db"))
    monkeypatch.setattr(
        "ultimate_coders.dashboard.metrics._ALERTS_DB_PATH", str(tmp_path / "metrics.db")
    )
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    publisher = AsyncMock()
    dashboard = DashboardApp(orchestrator=None, nats_publisher=publisher)
    config = {"inference_task": {"task_type": "port_model"}}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard._app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/dashboard/api/tasks/submit", json={"description": "port", "agent_config": config}
            )
            assert response.status_code == 200
            assert publisher.publish_submit.call_args.kwargs["agent_config"] == config
            publisher.publish_submit.reset_mock()
            response = await client.post(
                "/dashboard/api/tasks/submit", json={"description": "port", "agent_config": "bad"}
            )
            assert response.status_code == 400
            publisher.publish_submit.assert_not_called()
    finally:
        dashboard._metrics.close()
