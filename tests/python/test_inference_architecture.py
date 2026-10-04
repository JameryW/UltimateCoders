"""Architecture boundaries: controls, ownership, delivery and cross-host evidence."""

import asyncio
import base64
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from ultimate_coders.inference.adapter import MetaInferAdapter, RemoteStateUncertainError
from ultimate_coders.inference.artifact_store import ArtifactStore
from ultimate_coders.inference.hardware import capture_environment, condition_drift
from ultimate_coders.inference.reconcile import RemoteJobs
from ultimate_coders.inference.resources import ResourceBudget
from ultimate_coders.inference.service_contract import (
    DeploymentContractError,
    validate_quiescence,
    verify_hardware,
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
