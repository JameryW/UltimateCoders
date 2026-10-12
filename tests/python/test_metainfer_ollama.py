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
    observed = {"digest": "a" * 64, "chunks": [], "requests": [], "trickle": None,
                "responses": None}
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
            chunks = observed["responses"].pop(0) if observed["responses"] else observed["chunks"]
            for chunk in chunks:
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
    assert peer["requests"][0]["options"]["num_ctx"] == 16384
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


@pytest.mark.parametrize("tool", [False, True])
def test_output_exhaustion_refuses_nonempty_text_and_complete_tool_calls(ollama_peer, tool):
    profile, peer = ollama_peer
    message = {"content": "This response was truncated."}
    if tool:
        message["tool_calls"] = [{"function": {"name": "write_file", "arguments": {
            "path": "review.md", "content": "Verdict: PASS\n"}}}]
    peer["chunks"] = [{"model": profile.model, "message": message,
                       "done": True, "done_reason": "length"}]
    with pytest.raises(ValueError):
        profile.chat([], tools=[])


@pytest.mark.parametrize("context", [1024, 4096, 32768])
def test_explicit_context_profile_reaches_the_actual_provider_request(ollama_peer, context):
    profile, peer = ollama_peer
    peer["chunks"] = [
        {"model": "coding:test", "message": {"content": "verified"}, "done": True}
    ]
    configured = OllamaProfile.from_environment(
        {
            "UC_OLLAMA_URL": profile.url,
            "UC_OLLAMA_MODEL": profile.model,
            "UC_OLLAMA_DIGEST": profile.digest,
            "UC_OLLAMA_NUM_CTX": str(context),
        }
    )
    configured.chat([], tools=[])
    assert peer["requests"][0]["options"]["num_ctx"] == context
    assert peer["requests"][0]["model"] == profile.model
    assert configured.identity()["digest"] == profile.digest


def test_explicit_output_budget_reaches_provider_without_changing_legacy_default(ollama_peer):
    profile, peer = ollama_peer
    peer["chunks"] = [
        {"model": profile.model, "message": {"content": "verified"}, "done": True}
    ]
    profile.chat([], tools=[])
    assert peer["requests"][0]["options"]["num_predict"] == 4096
    configured = OllamaProfile.from_environment({
        "UC_OLLAMA_URL": profile.url, "UC_OLLAMA_MODEL": profile.model,
        "UC_OLLAMA_DIGEST": profile.digest, "UC_OLLAMA_NUM_PREDICT": "8192",
    })
    configured.chat([], tools=[])
    assert peer["requests"][1]["options"]["num_predict"] == 8192


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


@pytest.mark.parametrize("context", [0, 512, 32769, True, "4096"])
def test_profile_refuses_unbounded_or_non_integer_context_before_http(context):
    with pytest.raises(ValueError, match="profile"):
        OllamaProfile("http://localhost", "coding:test", "a" * 64, num_ctx=context)


@pytest.mark.parametrize("budget", [0, 127, 8193, True, "8192"])
def test_profile_refuses_unbounded_or_non_integer_output_budget(budget):
    with pytest.raises(ValueError, match="profile"):
        OllamaProfile("http://localhost", "coding:test", "a" * 64, num_predict=budget)


@pytest.mark.skipif(os.name != "posix", reason="Native sessions require Linux no-follow files")
def test_native_session_persists_effective_context(tmp_path, ollama_peer):
    from ultimate_coders.metainfer_service.coding import save_session

    profile, _ = ollama_peer
    configured = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=4096)
    save_session(tmp_path, "context-test", configured, [{"role": "user", "content": "saved"}])
    assert json.loads((tmp_path / "context-test.json").read_text())["num_ctx"] == 4096
    assert json.loads((tmp_path / "context-test.json").read_text())["num_predict"] == 4096


@pytest.mark.skipif(os.name != "posix", reason="Native sessions require Linux no-follow files")
def test_native_session_resumes_same_context_and_refuses_changed_context(tmp_path, ollama_peer):
    from ultimate_coders.metainfer_service.coding import load_session, save_session

    profile, peer = ollama_peer
    configured = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=4096)
    messages = [{"role": "user", "content": "saved"}]
    save_session(tmp_path, "context-test", configured, messages)
    assert load_session(tmp_path, "context-test", configured) == messages
    changed = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=8192)
    with pytest.raises(ValueError, match="context"):
        load_session(tmp_path, "context-test", changed)
    changed_budget = OllamaProfile(
        profile.url, profile.model, profile.digest, num_ctx=4096, num_predict=8192
    )
    with pytest.raises(ValueError, match="output budget"):
        load_session(tmp_path, "context-test", changed_budget)
    assert peer["requests"] == []


@pytest.mark.skipif(os.name != "posix", reason="Native sessions require Linux no-follow files")
def test_legacy_native_session_resumes_only_with_legacy_context(tmp_path, ollama_peer):
    from ultimate_coders.metainfer_service.coding import load_session

    profile, _ = ollama_peer
    messages = [{"role": "user", "content": "legacy"}]
    (tmp_path / "legacy.json").write_text(
        json.dumps({"identity": profile.identity(), "messages": messages})
    )
    assert load_session(tmp_path, "legacy", profile) == messages
    changed = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=4096)
    with pytest.raises(ValueError, match="context"):
        load_session(tmp_path, "legacy", changed)


@pytest.mark.skipif(os.name != "posix", reason="Native artifacts require Linux no-follow files")
def test_native_file_tool_writes_real_bounded_artifact_and_preserves_permissions(tmp_path):
    import hashlib

    from ultimate_coders.metainfer_service.coding import write_file

    text = "Verdict: FAIL\nObserved reference execution failure.\n"
    first = write_file("reviews/result.md", text, tmp_path)
    target = tmp_path / "reviews/result.md"
    assert target.read_text() == text
    assert first["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert first["kind"] == "add"
    target.chmod(0o640)
    updated = write_file(str(target), "Verdict: PASS\n", tmp_path)
    assert updated["kind"] == "update"
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name != "posix", reason="Native artifacts require Linux no-follow files")
def test_native_file_tool_refuses_escape_symlinks_and_special_files(tmp_path):
    from ultimate_coders.metainfer_service.coding import write_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.md"
    sentinel.write_text("unchanged")
    (workspace / "alias").symlink_to(outside, target_is_directory=True)
    (workspace / "linked.md").symlink_to(sentinel)
    os.mkfifo(workspace / "fifo")
    for path in ("../outside/keep.md", str(sentinel), "alias/keep.md", "linked.md", "fifo"):
        with pytest.raises((ValueError, OSError)):
            write_file(path, "changed", workspace)
    assert sentinel.read_text() == "unchanged"
    with pytest.raises(ValueError, match="bound"):
        write_file("oversized.md", "x" * 65537, workspace)
    assert not (workspace / "oversized.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native artifacts require Linux no-follow files")
def test_native_file_tool_closes_directory_when_temporary_cleanup_fails(tmp_path, monkeypatch):
    from ultimate_coders.metainfer_service import coding

    opened = []
    original_open = os.open

    def record_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_unlink(*args, **kwargs):
        raise PermissionError("Observed cleanup refusal")

    monkeypatch.setattr(coding.os, "open", record_open)
    monkeypatch.setattr(coding.os, "unlink", fail_unlink)
    with pytest.raises(PermissionError, match="cleanup refusal"):
        coding.write_file("review.md", "Actual bytes\n", tmp_path)
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="Native CLI sessions require Linux")
def test_native_cli_executes_streamed_file_tool_and_emits_distinct_items(
    tmp_path, monkeypatch, ollama_peer, capsys, native_cli_sessions
):
    from ultimate_coders.metainfer_service import coding

    profile, peer = ollama_peer
    monkeypatch.setattr(coding, "require_containment", lambda: None)
    monkeypatch.setenv("UC_OLLAMA_URL", profile.url)
    monkeypatch.setenv("UC_OLLAMA_MODEL", profile.model)
    monkeypatch.setenv("UC_OLLAMA_DIGEST", profile.digest)
    peer["responses"] = [
        [{"model": profile.model, "done": True, "message": {"tool_calls": [
            {"function": {"name": "write_file", "arguments": {
                "path": "review.md", "content": "Verdict: FAIL\n"}}}
        ]}}],
        [{"model": profile.model, "done": True, "message": {"content": "Created review.md"}}],
    ]
    assert coding.main(["exec", "-C", str(tmp_path), "Write review.md with write_file."]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    thread = next(item["thread_id"] for item in events if item["type"] == "thread.started")
    (native_cli_sessions / (thread + ".json")).unlink()
    assert (tmp_path / "review.md").read_text() == "Verdict: FAIL\n"
    items = [item["item"] for item in events if item["type"] == "item.completed"]
    assert [item["type"] for item in items] == ["file_change", "agent_message"]
    assert len({item["id"] for item in items}) == len(items)
    assert events[-1]["actual_tools_executed"] == 1
    assert peer["requests"][1]["messages"][-1]["tool_name"] == "write_file"


@pytest.mark.skipif(os.name != "posix", reason="Native CLI sessions require Linux")
@pytest.mark.parametrize("tool", [False, True])
def test_native_cli_never_writes_or_completes_an_exhausted_response(
    tmp_path, monkeypatch, ollama_peer, capsys, tool, native_cli_sessions
):
    from ultimate_coders.metainfer_service import coding

    profile, peer = ollama_peer
    monkeypatch.setattr(coding, "require_containment", lambda: None)
    monkeypatch.setenv("UC_OLLAMA_URL", profile.url)
    monkeypatch.setenv("UC_OLLAMA_MODEL", profile.model)
    monkeypatch.setenv("UC_OLLAMA_DIGEST", profile.digest)
    message = {"content": "A nonempty but exhausted response."}
    if tool:
        message["tool_calls"] = [{"function": {"name": "write_file", "arguments": {
            "path": "review.md", "content": "Verdict: PASS\n"}}}]
    peer["responses"] = [
        [{"model": profile.model, "message": message, "done": True, "done_reason": "length"}],
        [{"model": profile.model, "message": {"content": "Completed."}, "done": True}],
    ]
    try:
        with pytest.raises(ValueError):
            coding.main(["exec", "-C", str(tmp_path), "Write review.md."])
    finally:
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        thread = next(item["thread_id"] for item in events if item["type"] == "thread.started")
        (native_cli_sessions / (thread + ".json")).unlink(missing_ok=True)
    assert not (tmp_path / "review.md").exists()
    assert not any(item["type"] in ("item.completed", "turn.completed") for item in events)


@pytest.fixture
def native_cli_sessions(tmp_path, monkeypatch):
    """Keep real session I/O in this test's directory, away from live OCI jobs."""
    from pathlib import Path

    from ultimate_coders.metainfer_service import coding

    sessions = tmp_path / "sessions"

    class IsolatedPath(type(Path())):
        def __new__(cls, *args):
            if args == ("/tmp/uc-ollama-sessions",):
                return sessions
            return super().__new__(cls, *args)

    monkeypatch.setattr(coding, "Path", IsolatedPath)
    return sessions


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


def test_attested_context_changes_require_quiescent_ownership(attested_fixture):
    from ultimate_coders.metainfer_service.jobs import ContainedJobs

    config, jobs, runtime, environment, _ = attested_fixture
    configured = {**environment, "UC_OLLAMA_NUM_CTX": "4096"}
    jobs.enable_hardware(configured)
    assert jobs.hardware_attestation.profile.num_ctx == 4096
    jobs.ownership.mutate("workspace_owners", "active", lambda _: {"task_id": "live-job"})
    recovered = ContainedJobs(config, runtime, cgroup_root=jobs.cgroup_root)
    with pytest.raises(RuntimeError, match="Quiesce"):
        recovered.enable_hardware({**configured, "UC_OLLAMA_NUM_CTX": "8192"})
    assert runtime.calls == ["create", "start"]


@pytest.mark.parametrize(
    "key,value", [("UC_OLLAMA_NUM_CTX", "4096"), ("UC_OLLAMA_NUM_PREDICT", "8192")]
)
def test_implicit_attested_context_cannot_be_overridden_before_launch(attested_fixture, key, value):
    config, jobs, runtime, environment, _ = attested_fixture
    jobs.enable_hardware(environment)
    state = config.control_dir.parent / "context-override-state"
    with pytest.raises(ValueError, match="attested"):
        jobs.launch(
            "context-override",
            config.workspace_roots[0] / "job",
            state,
            config.control_dir.parent / "context-override-generated",
            ["python", "-c", "pass"],
            {**environment, key: value},
        )
    assert runtime.calls == ["create", "start"]
    assert not state.exists()
    assert jobs.ownership.query("workspace_owners", task_id_present=True, limit=1) == []


def test_hardware_code_mismatch_keeps_endpoint_unavailable_after_verified_stop(attested_fixture):
    _, jobs, runtime, environment, _ = attested_fixture
    runtime.corrupt_code = True
    with pytest.raises(ValueError, match="matching GPU/model"):
        jobs.enable_hardware(environment)
    with pytest.raises(RuntimeError, match="not been configured"):
        jobs.hardware()
    assert runtime.calls == ["create", "start"]
    assert jobs.ownership.query("workspace_owners", task_id_present=True, limit=1) == []


@pytest.mark.parametrize("delay, expected_status", [(2.3, 200), (6, 503)])
def test_hardware_http_accepts_a_cold_query_but_bounds_stalled_identity(
    attested_fixture, delay, expected_status
):
    import shlex
    import sys

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ultimate_coders.metainfer_service.app import add_contract_routes

    config, jobs, runtime, environment, _ = attested_fixture
    jobs.enable_hardware(environment)
    # This file is owned by attested_fixture inside its isolated temporary PATH.
    smi = config.control_dir.parent / "bin/nvidia-smi"
    assert smi.resolve().parent == config.control_dir.parent / "bin"
    code = f"import time; time.sleep({delay}); print('GPU-fixture, Fixture GPU, 1, 100')"
    smi.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)}\n")
    app = FastAPI()
    add_contract_routes(app, config, jobs)
    started = time.monotonic()
    with TestClient(app) as client:
        response = client.get("/api/uc/hardware")
    assert response.status_code == expected_status
    assert time.monotonic() - started < 6
    assert runtime.calls == ["create", "start"]
