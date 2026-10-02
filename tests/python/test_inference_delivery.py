"""Inference task delivery across planner, sandbox, Git and remote HTTP seams."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.sandbox import SandboxConfig
from ultimate_coders.agent.worker import Worker
from ultimate_coders.agent.workspace import WorkspaceManager


@pytest.fixture
def inference_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text(".uc/\n__pycache__/\n")
    (root / "latency.txt").write_text("43")
    (root / "bench.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "print(json.dumps({'workload_id':'fixture','compile_success':True,'correctness':True,"
        "'metrics':{'tpot_ms':float(Path('latency.txt').read_text())}}))\n"
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    return root


@pytest.fixture
def candidate_service():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.endswith("/schema"):
                self.reply(200, {"fields": [{"key": "kernel_file_path", "required": True}]})
            elif self.path == "/api/sys-shell/candidate":
                self.reply(200, {"run": {"finished": True, "final_status": "completed"}})
            else:
                self.reply(404, {})

        def do_POST(self):  # noqa: N802
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            kernel = Path(data["answers"]["kernel_file_path"])
            paths.append(str(kernel))
            kernel.write_text("37")
            self.reply(200, {"task_id": "candidate"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", paths
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


async def domain_subtask(root):
    task = await Orchestrator(llm_client=AsyncMock()).submit_task(
        "Optimize the kernel",
        agent_config={
            "inference_task": {
                "task_type": "optimize_kernel",
                "repository": str(root),
                "objective": "minimize TPOT",
                "parameters": {"kernel_file_path": "latency.txt"},
            },
            "benchmark": {
                "command": [sys.executable, "-B", "bench.py"],
                "workload_id": "fixture",
                "protected_paths": ["bench.py"],
            },
        },
    )
    return task.subtasks[0]


@pytest.mark.asyncio
async def test_explicit_inference_is_isolated_and_delivered(
    inference_repository,
    candidate_service,
    monkeypatch,
    tmp_path,
):
    root = inference_repository
    url, paths = candidate_service
    monkeypatch.setenv("UC_METAINFER_URL", url)
    monkeypatch.setenv("UC_INFERENCE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    manager = WorkspaceManager(str(root))
    worker = Worker(
        sandbox_config=SandboxConfig(
            project_path=str(root),
            env_vars={"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "python")},
        ),
        workspace_manager=manager,
    )

    result = await worker.execute_subtask(await domain_subtask(root))

    assert result.success, result.summary
    assert len(paths) == 1
    assert Path(paths[0]).parent != root
    assert ".uc/worktrees/" in paths[0].replace("\\", "/")
    assert (root / "latency.txt").read_text() == "37"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    assert not status.stdout


@pytest.mark.asyncio
async def test_inference_allocation_failure_never_executes_shared_code(inference_repository):
    manager = WorkspaceManager(str(inference_repository))
    manager.acquire = AsyncMock(return_value=None)
    worker = Worker(
        sandbox_config=SandboxConfig(project_path=str(inference_repository)),
        workspace_manager=manager,
    )
    worker._sandbox_manager.execute = AsyncMock()
    result = await worker.execute_subtask(await domain_subtask(inference_repository))
    assert not result.success
    assert not result.retryable
    worker._sandbox_manager.execute.assert_not_awaited()
    assert (inference_repository / "latency.txt").read_text() == "43"


@pytest.mark.asyncio
async def test_strict_worktree_head_fallback_sets_its_path(inference_repository):
    manager = WorkspaceManager(str(inference_repository), base_branch="absent-branch")
    handle = await manager.acquire("domain-task", require_worktree=True)
    assert handle is not None
    assert Path(handle.worktree_path).is_dir()
    assert Path(handle.worktree_path) != inference_repository
    assert (Path(handle.worktree_path) / "latency.txt").read_text() == "43"
    await manager.release(handle, merge=False)


@pytest.mark.asyncio
async def test_delivery_recovery_does_not_reexecute_after_removed_worktree(inference_repository):
    from ultimate_coders.agent.types import SubtaskResult

    manager = WorkspaceManager(str(inference_repository))
    worker = Worker(
        sandbox_config=SandboxConfig(project_path=str(inference_repository)),
        workspace_manager=manager,
    )
    subtask = await domain_subtask(inference_repository)
    handle = await manager.acquire(subtask.id, require_worktree=True)
    (Path(handle.worktree_path) / "latency.txt").write_text("37")
    await worker._save_checkpoint(
        subtask,
        SubtaskResult(
            subtask_id=subtask.id,
            success=True,
            summary="accepted",
            domain_result={"experiment_id": "fixture"},
        ),
        delivery="pending",
    )
    delivery = await manager.release(handle)
    assert delivery["status"] == "merged"
    assert not Path(handle.worktree_path).exists()
    restarted = Worker(sandbox_config=SandboxConfig(project_path=str(inference_repository)))
    restarted._sandbox_manager.execute = AsyncMock()
    result = await restarted.execute_subtask(subtask)
    assert result.success
    assert result.domain_result["delivery"]["commit_sha"] == delivery["commit_sha"]
    restarted._sandbox_manager.execute.assert_not_awaited()
    assert (inference_repository / "latency.txt").read_text() == "37"


@pytest.mark.asyncio
async def test_reused_pid_does_not_block_dead_workspace_owner(inference_repository):
    manager = WorkspaceManager(str(inference_repository))
    handle = await manager.acquire("recovery", require_worktree=True)
    manager._state.mutate("workspace_leases", handle.lease_key,
                          lambda old: {**old, "process_identity": "previous-process-birth"})
    restarted = WorkspaceManager(str(inference_repository))
    adopted = await restarted.acquire("recovery", require_worktree=True)
    assert adopted.worktree_path == handle.worktree_path
    await restarted.release(adopted, merge=False)


@pytest.mark.asyncio
async def test_runner_start_and_workspace_takeover_share_lease_transaction(
    inference_repository, monkeypatch, tmp_path,
):
    from ultimate_coders.inference.adapter import RemoteStateUncertainError
    from ultimate_coders.inference.runner import run_inference

    manager = WorkspaceManager(str(inference_repository))
    handle = await manager.acquire("spawn-boundary", require_worktree=True)
    owner = await manager.runner_owner(handle.worktree_path)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(manager._state.path.parent))
    monkeypatch.setenv("UC_INFERENCE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    execute = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("ultimate_coders.inference.runner._execute_inference", execute)
    request = {"cwd": handle.worktree_path, "config": {}, "workspace_owner": owner}
    await run_inference(request)
    manager._state.mutate("workspace_leases", handle.lease_key,
                          lambda old: {**old, "process_identity": "dead-parent"})
    restarted = WorkspaceManager(str(inference_repository))
    assert await restarted.acquire("spawn-boundary", require_worktree=True) is None
    # A child delayed until after its parent dies must never execute.
    with pytest.raises(RemoteStateUncertainError):
        await run_inference(request)
    execute.assert_awaited_once()
    await manager.release(handle, merge=False)


@pytest.mark.asyncio
async def test_stale_child_cannot_execute_after_worktree_takeover(
    inference_repository, monkeypatch, tmp_path,
):
    from ultimate_coders.inference.adapter import RemoteStateUncertainError
    from ultimate_coders.inference.runner import run_inference

    manager = WorkspaceManager(str(inference_repository))
    handle = await manager.acquire("delayed-child", require_worktree=True)
    owner = await manager.runner_owner(handle.worktree_path)
    manager._state.mutate("workspace_leases", handle.lease_key,
                          lambda old: {**old, "process_identity": "dead-parent"})
    restarted = WorkspaceManager(str(inference_repository))
    adopted = await restarted.acquire("delayed-child", require_worktree=True)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(manager._state.path.parent))
    monkeypatch.setenv("UC_INFERENCE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    execute = AsyncMock()
    monkeypatch.setattr("ultimate_coders.inference.runner._execute_inference", execute)
    with pytest.raises(RemoteStateUncertainError):
        await run_inference({"cwd": handle.worktree_path, "config": {}, "workspace_owner": owner})
    execute.assert_not_awaited()
    await restarted.release(adopted, merge=False)
