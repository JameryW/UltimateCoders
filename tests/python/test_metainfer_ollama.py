"""Native Ollama protocol checks use a real local HTTP peer."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from ultimate_coders.metainfer_service.ollama import OllamaProfile


@pytest.fixture
def ollama_peer():
    observed = {"digest": "a" * 64, "chunks": [], "requests": [], "trickle": None}
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 -- HTTP interface
            payload = {"models": [{"name": "coding:test", "digest": observed["digest"]}]}
            self.send_response(200)
            self.end_headers()
            if observed["trickle"] == "tags":
                self.trickle()
                return
            self.wfile.write(json.dumps(payload).encode())

        def do_POST(self):  # noqa: N802 -- HTTP interface
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed["requests"].append(payload)
            self.send_response(200)
            self.end_headers()
            if observed["trickle"] == "chat":
                self.trickle()
                return
            for chunk in observed["chunks"]:
                self.wfile.write(json.dumps(chunk).encode() + b"\n")

        def trickle(self):
            try:
                while not stop.wait(0.02):
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            OllamaProfile(f"http://127.0.0.1:{server.server_port}", "coding:test", "a" * 64),
            observed,
        )
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_chat_uses_pinned_model_and_preserves_actual_streamed_tools(ollama_peer):
    profile, peer = ollama_peer
    call = {"function": {"name": "exec_command", "arguments": {"cmd": "echo verified"}}}
    peer["chunks"] = [
        {"model": "coding:test", "message": {"content": "Run", "tool_calls": [call]}},
        {"model": "coding:test", "message": {"content": " complete"}, "done": True},
    ]
    result = profile.chat([{"role": "user", "content": "Verify"}], tools=[])
    assert result["message"]["content"] == "Run complete"
    assert result["message"]["tool_calls"] == [call]
    assert peer["requests"][0]["model"] == "coding:test"
    peer["digest"] = "b" * 64
    with pytest.raises(ValueError, match="digest"):
        profile.chat([], tools=[])
    assert len(peer["requests"]) == 1


@pytest.mark.parametrize(
    "chunks",
    [
        [{"model": "other:model", "message": {}, "done": True}],
        [{"model": "coding:test", "message": {"content": "unfinished"}}],
    ],
)
def test_chat_refuses_changed_model_and_incomplete_stream(ollama_peer, chunks):
    profile, peer = ollama_peer
    peer["chunks"] = chunks
    with pytest.raises(ValueError):
        profile.chat([], tools=[])


def test_text_describing_a_tool_is_preserved_as_text(ollama_peer):
    profile, peer = ollama_peer
    peer["chunks"] = [
        {"model": "coding:test", "message": {"content": "exec_command(echo ok)"}, "done": True}
    ]
    result = profile.chat([], tools=[])
    assert result["message"]["tool_calls"] == []


def test_profile_does_not_inherit_host_proxy_settings(ollama_peer, monkeypatch):
    profile, _ = ollama_peer
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert profile.identity()["digest"] == "a" * 64


@pytest.mark.parametrize("operation", ["tags", "chat"])
def test_absolute_deadline_terminates_a_peer_that_keeps_trickling_without_newlines(
    ollama_peer, operation
):
    profile, peer = ollama_peer
    peer["trickle"] = operation
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        if operation == "tags":
            profile.identity(timeout=0.5)
        else:
            profile.chat([], tools=[], timeout=0.5)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    "url", ["http://user:secret@localhost", "http://localhost/api", "file:///tmp/model"]
)
def test_profile_refuses_credentials_and_non_provider_urls(url):
    with pytest.raises(ValueError):
        OllamaProfile(url, "coding:test", "a" * 64)


@pytest.mark.skipif(os.name != "posix", reason="Native client executes only inside Linux OCI")
@pytest.mark.asyncio
async def test_native_command_deadline_kills_its_writer_before_returning(tmp_path):
    import asyncio

    from ultimate_coders.metainfer_service.coding import execute_command

    result = await execute_command(
        "(sleep 1; echo escaped > late-write) & wait", tmp_path, timeout=0.1
    )
    assert result["exit_code"] == 124
    await asyncio.sleep(1.2)
    assert not (tmp_path / "late-write").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native commands require Linux")
@pytest.mark.asyncio
async def test_expired_turn_cannot_start_the_next_tool_in_a_batch(tmp_path):
    import time

    from ultimate_coders.metainfer_service.coding import execute_command

    deadline = time.monotonic() + 0.1
    result = await execute_command("sleep 5", tmp_path, deadline=deadline)
    assert result["exit_code"] == 124
    with pytest.raises(TimeoutError, match="before tool launch"):
        await execute_command("echo late > never-started", tmp_path, deadline=deadline)
    assert not (tmp_path / "never-started").exists()


@pytest.fixture
def attested_fixture(tmp_path, monkeypatch, ollama_peer):
    """Emulated OCI/device boundaries, explicitly not GPU acceptance evidence."""
    if os.name != "posix":
        pytest.skip("Private Linux OCI evidence")
    from pathlib import Path

    from ultimate_coders.metainfer_service.app import ServiceConfig
    from ultimate_coders.metainfer_service.jobs import ContainedJobs
    from ultimate_coders.metainfer_service.ollama import native_client_identity

    from .test_metainfer_service import RuntimeFixture

    profile, peer = ollama_peer
    workspace = tmp_path / "workspaces/job"
    workspace.mkdir(parents=True)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    binary = tmp_path / "bin"
    binary.mkdir()
    smi = binary / "nvidia-smi"
    smi.write_text("#!/bin/sh\nprintf 'GPU-fixture, Fixture GPU, 1, 100\\n'\n")
    smi.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + ":" + os.environ["PATH"])

    class Runtime(RuntimeFixture):
        image_labels, gpu = {"uc.metainfer.ollama": "1"}, "wsl"
        corrupt_code = False

        def create(self, name, labels, mounts, command, environment):
            self.gate = Path(command[4])
            report = {
                "devices": [
                    {
                        "uuid": "GPU-fixture",
                        "model": "Fixture GPU",
                        "driver": "1",
                        "memory_mib": "100",
                    }
                ],
                "runtime": {"torch": "fixture", "image_code": native_client_identity()},
                "model_identity": {
                    "provider": "ollama",
                    "model": profile.model,
                    "digest": profile.digest,
                },
                "gpu_cases": [{"size": 33, "max_error": 0}],
                "model_probe": {"nonce": "fixture"},
            }
            if self.corrupt_code:
                report["runtime"]["image_code"]["coding.py"] = "f" * 64
            (mounts[1][0] / "hardware.json").write_text(json.dumps(report))
            return super().create(name, labels, mounts, command, environment)

        def inspect(self, container_id):
            if self.gate.exists():
                self.running = False
            return super().inspect(container_id)

    runtime = Runtime(cgroup)
    config = ServiceConfig(tmp_path / "control", (workspace.parent,), "backend-a", "revision-a")
    jobs = ContainedJobs(config, runtime, cgroup_root=tmp_path)
    environment = {
        "METAINFER_AGENT_BACKEND": "codex",
        "METAINFER_CODEX_BIN": "/opt/uc/ultimate_coders/metainfer_service/coding.py",
        "UC_OLLAMA_URL": profile.url,
        "UC_OLLAMA_MODEL": profile.model,
        "UC_OLLAMA_DIGEST": profile.digest,
    }
    return config, jobs, runtime, environment, peer


def test_hardware_http_and_restart_cache_recheck_live_model_without_new_probe(attested_fixture):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ultimate_coders.metainfer_service.app import add_contract_routes
    from ultimate_coders.metainfer_service.jobs import ContainedJobs

    config, jobs, runtime, environment, peer = attested_fixture
    jobs.enable_hardware(environment)
    evidence = jobs.hardware()
    recovered = ContainedJobs(config, runtime, cgroup_root=jobs.cgroup_root)
    # Legacy ownership records carry no state; matching cache must not probe
    # while that writer remains owned.
    jobs.ownership.mutate("workspace_owners", "legacy-active", lambda _: {"task_id": "legacy"})
    recovered.enable_hardware(environment)
    assert recovered.hardware()["evidence_id"] == evidence["evidence_id"]
    assert runtime.calls == ["create", "start"]
    app = FastAPI()
    add_contract_routes(app, config, recovered)
    with TestClient(app) as client:
        assert client.get("/api/uc/hardware").json()["devices"][0]["uuid"] == "GPU-fixture"
        assert "hardware_identity" in client.get("/api/uc/contract").json()["capabilities"]
        peer["digest"] = "b" * 64
        assert client.get("/api/uc/hardware").status_code == 503
    with pytest.raises(ValueError, match="digest"):
        ContainedJobs(config, runtime, cgroup_root=jobs.cgroup_root).enable_hardware(environment)
    with pytest.raises(ValueError, match="digest"):
        recovered.launch(
            "new",
            config.workspace_roots[0] / "job",
            config.control_dir.parent / "new-state",
            config.control_dir.parent / "new-generated",
            ["python", "-c", "pass"],
            environment,
        )
    assert runtime.calls == ["create", "start"]


def test_re_attestation_refuses_legacy_ownership_without_a_state(attested_fixture):
    _, jobs, runtime, environment, _ = attested_fixture
    jobs.ownership.mutate("workspace_owners", "legacy", lambda _: {"task_id": "unresolved"})
    with pytest.raises(RuntimeError, match="Quiesce"):
        jobs.enable_hardware(environment)
    assert runtime.calls == []


def test_hardware_code_mismatch_keeps_endpoint_unavailable_after_verified_stop(attested_fixture):
    _, jobs, runtime, environment, _ = attested_fixture
    runtime.corrupt_code = True
    with pytest.raises(ValueError, match="matching GPU/model"):
        jobs.enable_hardware(environment)
    with pytest.raises(RuntimeError, match="not been configured"):
        jobs.hardware()
    assert runtime.calls == ["create", "start"]
    assert jobs.ownership.query("workspace_owners", task_id_present=True, limit=1) == []
