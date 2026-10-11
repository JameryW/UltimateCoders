"""Architecture boundaries: controls, ownership, delivery and cross-host evidence."""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from ultimate_coders.inference import service_contract
from ultimate_coders.inference.adapter import MetaInferAdapter, RemoteStateUncertainError
from ultimate_coders.inference.artifact_store import ArtifactStore
from ultimate_coders.inference.hardware import capture_environment, condition_drift
from ultimate_coders.inference.reconcile import RemoteJobs
from ultimate_coders.inference.resources import ResourceBudget
from ultimate_coders.inference.service_contract import (
    DeploymentContractError,
    validate_quiescence,
    verify_hardware,
    verify_workspace,
)
from ultimate_coders.outbox import OutcomeOutbox
from ultimate_coders.runtime_state import RecordConflictError, RuntimeState


@pytest.fixture
def state(tmp_path):
    return RuntimeState(tmp_path / "state.db", database_url="")


def _proof(backend="backend", job="job", **values):
    return {
        "contract_version": "uc-metainfer/1",
        "backend_id": backend,
        "task_id": job,
        "writers_stopped": True,
        "proof_kind": "cgroup_empty",
        "execution_scope": "job-container-identity",
        "evidence_id": "receipt-1",
        **values,
    }


def test_stop_acknowledgement_and_finished_metadata_are_not_stop_proofs():
    for proof in (
        {"ok": True},
        {"finished": True},
        _proof(writers_stopped=False),
        _proof(job="different"),
        _proof(proof_kind="signal_sent"),
    ):
        with pytest.raises(DeploymentContractError):
            validate_quiescence(proof, "job", "backend")
    assert validate_quiescence(_proof(), "job", "backend")["evidence_id"] == "receipt-1"


@pytest.mark.asyncio
async def test_kill_ack_without_all_writer_proof_keeps_reservations(state):
    started = asyncio.Event()

    async def service(request):
        if request.url.path.endswith("schema"):
            return httpx.Response(200, json={"fields": []})
        if request.url.path.endswith("control"):
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("quiescence"):
            return httpx.Response(404, json={})
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "job"})
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        adapter = MetaInferAdapter(
            "http://service", client=client, state=state, operation_id="op", backend_id="backend"
        )
        running = asyncio.create_task(adapter.analyze_trace(repository="repo", objective="trace"))
        await started.wait()
        running.cancel()
        with pytest.raises(RemoteStateUncertainError):
            await running
    assert state.get("remote_jobs", "op")["state"] == "cleanup_pending"
    assert "op" in state.get("resource_budgets", "backend")["holders"]
    assert state.get("backend_slots", "backend:0")["operation_id"] == "op"


@pytest.mark.asyncio
async def test_waiting_capacity_is_cancellable_and_does_not_release_active_writer(state):
    budget = ResourceBudget(state, "backend")
    await budget.acquire("first", timeout=10)
    waiting = asyncio.create_task(budget.acquire("second", timeout=10))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(waiting), 0.1)
    assert [item["operation_id"] for item in state.get("resource_budgets", "backend")["queue"]] == [
        "second"
    ]
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    record = state.get("resource_budgets", "backend")
    assert record["queue"] == []
    assert "first" in record["holders"]


@pytest.mark.asyncio
async def test_fifo_queue_and_restart_do_not_overreserve(state):
    budget = ResourceBudget(state, "stable-backend")
    await budget.acquire("first", timeout=10)
    second = asyncio.create_task(budget.acquire("second", timeout=10))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(second), 0.1)
    budget.release("first")
    assert (await asyncio.wait_for(second, 2)).operation_id == "second"
    restarted = ResourceBudget(RuntimeState(state.path, database_url=""), "stable-backend")
    await restarted.acquire("second", timeout=1)
    assert list(state.get("resource_budgets", "stable-backend")["holders"]) == ["second"]
    restarted.release("second")


def test_large_delivered_history_is_excluded_from_pending_pages(state):
    with sqlite3.connect(state.path) as conn:
        conn.executemany(
            "INSERT INTO uc_runtime_records(namespace,record_key,data,record_state) "
            "VALUES (?,?,?,?)",
            (
                ("result_outbox", f"old-{index:06}", '{"delivered":true}', "delivered")
                for index in range(100_000)
            ),
        )
    for index in range(7):
        state.mutate(
            "result_outbox",
            f"pending-{index}",
            lambda _: {"event": {}, "update": {}, "delivered": False},
        )
    page = OutcomeOutbox(state).pending(3)
    assert [item["key"] for item in page] == ["pending-0", "pending-1", "pending-2"]
    assert state.statistics("result_outbox")["delivered"]["count"] == 100_000


def test_concurrent_delivery_claims_and_retention_keep_replay_tombstone(state):
    state.mutate(
        "result_outbox",
        "attempt",
        lambda _: {"event": {"type": "done"}, "update": {}, "delivered": False},
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = [
            claim
            for claim in pool.map(lambda _: OutcomeOutbox(state).claim("attempt"), range(8))
            if claim
        ]
    assert len(claims) == 1
    OutcomeOutbox(state).finish(claims[0], delivered=True)
    assert state.archive_delivered(older_than=time.time() + 1) == 1
    tombstone = RuntimeState(state.path, database_url="").get("result_outbox", "attempt")
    assert tombstone["delivered"] and tombstone["tombstone"]
    assert "event" not in tombstone
    assert OutcomeOutbox(state).pending() == []


@pytest.mark.asyncio
async def test_operator_stop_reconciliation_requires_evidence_and_current_version(state):
    state.mutate(
        "remote_jobs",
        "op",
        lambda _: {"state": "cleanup_pending", "remote_id": "job", "backend_id": "backend"},
    )
    await ResourceBudget(state, "backend").acquire("op", timeout=1)
    job = RemoteJobs(state, "http://service").get("op")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_proof()))
    ) as client:
        await RemoteJobs(state, "http://service").reconcile(
            "op", job.version, "confirm_stop", actor="operator", client=client
        )
        with pytest.raises(RecordConflictError):
            await RemoteJobs(state, "http://service").reconcile(
                "op", job.version, "confirm_stop", actor="other", client=client
            )
    assert state.get("resource_budgets", "backend")["holders"] == {}
    with sqlite3.connect(state.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM uc_runtime_audit").fetchone()[0] == 1


def test_artifact_download_uses_shared_content_after_worker_files_disappear(state, tmp_path):
    report = tmp_path / "report.json"
    report.write_text('{"success":true}')
    metadata = ArtifactStore(state).publish(tmp_path)["report.json"]
    report.unlink()
    assert (
        ArtifactStore(RuntimeState(state.path, database_url="")).read(metadata)
        == b'{"success":true}'
    )
    # A tampered hash is rejected on two separate layers, and both are pinned:
    # the descriptor that contradicts itself, and the stored bytes that no
    # longer match the content address both halves of the descriptor agree on.
    with pytest.raises(ValueError, match="Invalid artifact metadata"):
        ArtifactStore(state).read({**metadata, "sha256": "0" * 64})
    state.mutate(
        "artifact_blobs",
        metadata["artifact_id"],
        lambda old: {**(old or {}), "content": base64.b64encode(b"corrupted").decode()},
    )
    with pytest.raises(ValueError, match="integrity"):
        ArtifactStore(state).read(metadata)


def test_actual_gpu_identity_and_condition_drift_are_evidence(monkeypatch):
    monkeypatch.setattr(
        "ultimate_coders.inference.hardware.shutil.which",
        lambda name: "nvidia-smi" if name == "nvidia-smi" else None,
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        "ultimate_coders.inference.hardware.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="0, GPU-real, Actual GPU, 1.2, 8192, 40, 1000, 0\n"
        ),
    )
    evidence = capture_environment({"require_gpu": "true", "gpu_uuid": "GPU-real"})
    assert evidence["identity"]["devices"][0]["driver"] == "1.2"
    with pytest.raises(ValueError, match="actual hardware"):
        capture_environment({"gpu_uuid": "GPU-fake"})
    changed = {**evidence, "conditions": [{**evidence["conditions"][0], "sm_clock_mhz": "800"}]}
    assert "clock drift" in condition_drift(evidence, changed)[0]


@pytest.mark.asyncio
async def test_remote_gpu_contract_requires_identity_and_model():
    async def service(request):
        return httpx.Response(
            200,
            json={
                "contract_version": "uc-metainfer/1",
                "backend_id": "backend",
                "revision": "rev",
                "devices": [{"uuid": "GPU-1", "model": "RTX 4060", "driver": "1"}],
                "runtime": {"cuda": "12.6"},
                "model_identity": {"sha256": "weights"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        evidence = await verify_hardware(
            client, "http://service", "backend", expected_revision="rev", expected_model="4060"
        )
    assert evidence["devices"][0]["uuid"] == "GPU-1"


@pytest.mark.asyncio
async def test_hardware_contract_client_allows_bounded_device_and_model_observation():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 -- HTTP interface
            # A bounded device query plus provider observation can exceed 5s.
            # Exercise httpx's real socket deadline; an async mock cannot do so.
            threading.Event().wait(5.3)
            body = json.dumps(
                {
                    "contract_version": "uc-metainfer/1",
                    "backend_id": "backend",
                    "devices": [{"uuid": "GPU-fixture"}],
                    "runtime": {"cuda": "fixture"},
                    "model_identity": {"digest": "fixture"},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            evidence = await verify_hardware(
                client, f"http://127.0.0.1:{server.server_port}", "backend"
            )
        assert evidence["devices"][0]["uuid"] == "GPU-fixture"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.mark.asyncio
async def test_workspace_contract_client_waits_for_contained_probe_and_stop(tmp_path):
    nonce_files_present = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):  # noqa: N802 -- HTTP interface
            self.respond(
                {
                    "contract_version": "uc-metainfer/1",
                    "backend_id": "backend",
                    "revision": "rev",
                    "capabilities": ["workspace_probe", "quiescence"],
                }
            )

        def do_POST(self):  # noqa: N802 -- HTTP interface
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            source, target = Path(payload["read_path"]), Path(payload["write_path"])
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target.write_text(payload["write_token"])
            # Real transport regression: cold device/OCI launch and verified
            # stop can exceed 5s while the caller must retain the nonce files.
            threading.Event().wait(5.3)
            nonce_files_present.append(source.is_file() and target.is_file())
            self.respond({"read_sha256": digest, "writer_identity": "contained-fixture"})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            receipt = await verify_workspace(
                client,
                f"http://127.0.0.1:{server.server_port}",
                str(tmp_path),
                expected_revision="rev",
            )
        assert receipt["workspace_receipt"]["writer_identity"] == "contained-fixture"
        assert nonce_files_present == [True]
        assert not list(tmp_path.glob(".uc-probe-*"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["hardware", "workspace"])
async def test_contract_deadline_bounds_a_continuously_trickling_peer(
    tmp_path, monkeypatch, operation
):
    budget = "HARDWARE_HTTP_SECONDS" if operation == "hardware" else "WORKSPACE_HTTP_SECONDS"
    monkeypatch.setattr(service_contract, budget, 0.3, raising=False)
    observed = {"posts": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload, *, trickle=False):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + (10 if trickle else 0)))
            self.end_headers()
            try:
                if trickle:
                    for _ in range(10):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        # Exercise a live socket, keeping every idle read short.
                        threading.Event().wait(0.1)
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):  # noqa: N802 -- HTTP interface
            self.respond(
                {
                    "contract_version": "uc-metainfer/1",
                    "backend_id": "backend",
                    "revision": "rev",
                    "capabilities": ["workspace_probe", "quiescence", "workspace_probe_identity"],
                    "devices": [{"uuid": "fixture"}],
                    "runtime": {"cuda": "fixture"},
                    "model_identity": {"digest": "fixture"},
                },
                trickle=operation == "hardware",
            )

        def do_POST(self):  # noqa: N802 -- HTTP interface
            observed["posts"] += 1
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed["probe"] = payload
            source, target = Path(payload["read_path"]), Path(payload["write_path"])
            target.write_text(payload["write_token"])
            self.respond(
                {
                    "read_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "writer_identity": "fixture",
                    "quiescence": _proof(job=payload.get("probe_id", "legacy-probe")),
                },
                trickle=True,
            )

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            started = time.monotonic()
            if operation == "hardware":
                with pytest.raises(httpx.TimeoutException):
                    await verify_hardware(client, url, "backend")
            else:
                state = RuntimeState(tmp_path / "state.db", database_url="")
                adapter = MetaInferAdapter(url, client=client, state=state, operation_id="op")
                from ultimate_coders.inference.models import InferenceTaskType, MetaInferTask

                task = MetaInferTask(
                    task_type=InferenceTaskType.OPTIMIZE_KERNEL,
                    repository=str(tmp_path),
                    objective="fixture optimization",
                )
                with pytest.raises(DeploymentContractError) as error:
                    await adapter.execute(task)
                assert error.value.cleanup_pending and error.value.retryable is False
                probe = observed["probe"]
                assert Path(probe["read_path"]).is_file()
                assert Path(probe["write_path"]).is_file()
                record = state.get("remote_jobs", "op:workspace-probe")
                assert record["state"] == "cleanup_pending"
                assert record["remote_id"] == probe["probe_id"]
                assert record["backend_id"] == "backend"
                with pytest.raises(RemoteStateUncertainError):
                    await adapter.execute(task)
                assert observed["posts"] == 1
                version = RemoteJobs(state, url).get("op:workspace-probe").version
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json=_proof(job=probe["probe_id"]))
                    )
                ) as observer:
                    await RemoteJobs(state, url).reconcile(
                        "op:workspace-probe",
                        version,
                        "confirm_stop",
                        actor="operator",
                        client=observer,
                    )
                assert state.get("remote_jobs", "op:workspace-probe")["state"] == "stopped"
            assert time.monotonic() - started < 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.mark.asyncio
async def test_old_workspace_receipt_cannot_clear_a_new_probe_owner(state, tmp_path):
    from ultimate_coders.inference.models import InferenceTaskType, MetaInferTask

    def service(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "contract_version": "uc-metainfer/1",
                    "backend_id": "backend",
                    "revision": "rev",
                    "capabilities": ["workspace_probe", "quiescence"],
                },
            )
        probe = json.loads(request.content)
        source, target = Path(probe["read_path"]), Path(probe["write_path"])
        target.write_text(probe["write_token"])
        state.mutate(
            "remote_jobs",
            "op:workspace-probe",
            lambda old: {**old, "probe_directory": "new-owner", "state": "cleanup_pending"},
        )
        return httpx.Response(
            200,
            json={
                "read_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "writer_identity": "fixture",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        adapter = MetaInferAdapter("http://service", state=state, operation_id="op")
        task = MetaInferTask(InferenceTaskType.OPTIMIZE_KERNEL, str(tmp_path), "fixture")
        with pytest.raises(RemoteStateUncertainError, match="owner changed"):
            await adapter._preflight(client, task)
    record = state.get("remote_jobs", "op:workspace-probe")
    assert record["probe_directory"] == "new-owner" and record["state"] == "cleanup_pending"


class _StockUpstreamHandler(BaseHTTPRequestHandler):
    """Only the routes stock MetaInfer has -- no `/api/uc/*` at all.

    Verified against the pinned upstream revision `b3f6505a11ab`, which
    contains zero occurrences of `/api/uc`, `quiescence`, `workspace_probe`,
    `contract_version` or `uc-metainfer`. This stub is that shape.
    """

    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, code, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/sys-shell/task-types"):
            self._send(200, {"fields": []})
        else:
            self._send(404, {"detail": "Not Found"})


def _run_release_gate(url: str, workspace: Path, output: Path):
    """Run the real gate script the way the release workflow does."""
    env = dict(os.environ)
    pythonpath = str(Path(__file__).resolve().parents[2] / "python")
    env["PYTHONPATH"] = pythonpath + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts" / "verify-metainfer-release.py"),
            "--url", url,
            "--workspace", str(workspace),
            "--output", str(output),
            "--require-gpu",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_release_gate_rejects_a_contractless_service_with_a_reason_not_a_traceback(tmp_path):
    """R8: the rejection path is evidence too.

    Pointing the gate at stock upstream MetaInfer is the situation an operator
    faces today. It must answer with the contract it is missing, not with a
    stack trace -- measured before the fix as a bare
    `httpx.HTTPStatusError: Client error '404 Not Found' ... /api/uc/contract`,
    because the script fetched the contract itself with `raise_for_status()`
    before delegating to the one function that knows how to say it properly.
    """
    server = HTTPServer(("127.0.0.1", 0), _StockUpstreamHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        workspace = tmp_path / "shared-workspace"
        workspace.mkdir()
        (workspace / "probe-target.txt").write_text("shared workspace probe target")
        output = tmp_path / "evidence.json"
        result = _run_release_gate(f"http://127.0.0.1:{port}", workspace, output)
    finally:
        server.shutdown()

    # A refusal, not a crash: no interpreter traceback anywhere in the output.
    assert "Traceback (most recent call last)" not in result.stderr
    assert "httpx.HTTPStatusError" not in result.stderr
    assert result.returncode != 0, "a service without the contract must not pass the gate"

    # And the reason names the contract, so the release log says what was missing.
    assert "REJECTED" in result.stderr, result.stderr
    assert "UC workspace/stop contract" in result.stderr, result.stderr

    # No partial evidence is written for a rejected deployment.
    assert not output.exists(), "a rejected gate must not leave an evidence file"


def test_release_gate_names_an_unreachable_service_separately(tmp_path):
    """Transport failure is not a contract verdict; it says so and exits apart."""
    workspace = tmp_path / "shared-workspace"
    workspace.mkdir()
    output = tmp_path / "evidence.json"
    # 127.0.0.1 on a port nothing is listening on: connection refused.
    result = _run_release_gate("http://127.0.0.1:9", workspace, output)

    assert "Traceback (most recent call last)" not in result.stderr
    assert result.returncode != 0
    assert "REJECTED" in result.stderr, result.stderr
    assert "unreachable or unhealthy" in result.stderr, result.stderr
    assert not output.exists()
