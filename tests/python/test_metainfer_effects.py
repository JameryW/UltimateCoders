"""MetaInfer create POST through the effect ledger (UC_EFFECT_LEDGER_METAINFER).

Flag off: no tool_effects rows. Flag on: success → CONFIRMED, lost response →
UNKNOWN (not FAILED), and a prior CONFIRMED receipt skips a second POST.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from ultimate_coders.effect_ledger import (
    NAMESPACE,
    EffectLedger,
    EffectStatus,
    stable_effect_id,
)
from ultimate_coders.inference.adapter import (
    EFFECT_LEDGER_METAINFER_ENV,
    MetaInferAdapter,
    RemoteStateUncertainError,
)
from ultimate_coders.inference.models import InferenceTaskType, MetaInferTask
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return RuntimeState()


def _request_sha(task: MetaInferTask) -> str:
    return hashlib.sha256(json.dumps(task.to_dict(), sort_keys=True).encode()).hexdigest()


def _task() -> MetaInferTask:
    return MetaInferTask(
        InferenceTaskType.ANALYZE_TRACE,
        repository="repo",
        objective="trace",
    )


def _adapter(client, state, operation_id="op"):
    return MetaInferAdapter(
        "http://service",
        client=client,
        state=state,
        operation_id=operation_id,
        backend_id="backend-a",
    )


def _completed_poll(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("schema"):
        return httpx.Response(200, json={"fields": []})
    if request.method == "POST" and request.url.path.endswith("/tasks"):
        return httpx.Response(200, json={"task_id": "job-1"})
    if "/api/sys-shell/job-1" in request.url.path and request.method == "GET":
        return httpx.Response(200, json={"run": {"finished": True, "final_status": "completed"}})
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_flag_off_writes_no_tool_effects(state, monkeypatch):
    monkeypatch.delenv(EFFECT_LEDGER_METAINFER_ENV, raising=False)
    posts = []

    def service(request):
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            posts.append(request)
        return _completed_poll(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        result = await _adapter(client, state).analyze_trace(repository="repo", objective="trace")
    assert result.task_id == "job-1"
    assert state.records(NAMESPACE) == []
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_success_confirms_metainfer_create(state, monkeypatch):
    monkeypatch.setenv(EFFECT_LEDGER_METAINFER_ENV, "1")
    task = _task()
    effect_id = stable_effect_id("metainfer-create", "backend-a", "op", _request_sha(task))

    async with httpx.AsyncClient(transport=httpx.MockTransport(_completed_poll)) as client:
        result = await _adapter(client, state).analyze_trace(repository="repo", objective="trace")
    assert result.task_id == "job-1"
    record = EffectLedger(state).get(effect_id)
    assert record is not None
    assert record["kind"] == "metainfer-create"
    assert record["status"] == EffectStatus.CONFIRMED.value
    assert record["result"]["remote_id"] == "job-1"


@pytest.mark.asyncio
async def test_uncertain_post_marks_unknown_not_failed(state, monkeypatch):
    monkeypatch.setenv(EFFECT_LEDGER_METAINFER_ENV, "true")
    task = _task()
    effect_id = stable_effect_id("metainfer-create", "backend-a", "op", _request_sha(task))

    def service(request):
        if request.url.path.endswith("schema"):
            return httpx.Response(200, json={"fields": []})
        if request.method == "POST":
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        with pytest.raises(RemoteStateUncertainError):
            await _adapter(client, state).analyze_trace(repository="repo", objective="trace")
    record = EffectLedger(state).get(effect_id)
    assert record["status"] == EffectStatus.UNKNOWN.value
    assert record["status"] != EffectStatus.FAILED.value


@pytest.mark.asyncio
async def test_confirmed_prepare_skips_second_post(state, monkeypatch):
    """CONFIRMED receipt must suppress POST even when remote_jobs has no remote_id yet.

    Pre-seeding remote_jobs.remote_id would bypass the create block entirely via the
    early adopt path; the ledger short-circuit is what this test covers.
    """
    monkeypatch.setenv(EFFECT_LEDGER_METAINFER_ENV, "on")
    task = _task()
    request_sha = _request_sha(task)
    effect_id = stable_effect_id("metainfer-create", "backend-a", "op", request_sha)
    ledger = EffectLedger(state)
    ledger.prepare(effect_id, kind="metainfer-create")
    ledger.mark_in_flight(effect_id, owner="earlier")
    ledger.confirm(effect_id, result={"remote_id": "job-1"})
    posts = []

    def service(request):
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            posts.append(request)
            return httpx.Response(500, json={"error": "should not POST"})
        return _completed_poll(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        result = await _adapter(client, state).analyze_trace(repository="repo", objective="trace")
    assert result.task_id == "job-1"
    assert posts == []
    assert EffectLedger(state).get(effect_id)["status"] == EffectStatus.CONFIRMED.value
    # Operational store must catch up so cancel/reconcile can find the remote.
    assert state.get("remote_jobs", "op")["remote_id"] == "job-1"


@pytest.mark.asyncio
async def test_unknown_with_remote_jobs_proof_confirms_without_post(state, monkeypatch):
    """UNKNOWN receipt + remote_jobs.remote_id reconciles to CONFIRMED; no second POST.

    Early adopt takes remote_id from remote_jobs before the create block; the ledger
    must still settle so watchdog/preserve_effects see CONFIRMED.
    """
    monkeypatch.setenv(EFFECT_LEDGER_METAINFER_ENV, "1")
    task = _task()
    request_sha = _request_sha(task)
    effect_id = stable_effect_id("metainfer-create", "backend-a", "op", request_sha)
    ledger = EffectLedger(state)
    ledger.prepare(effect_id, kind="metainfer-create")
    ledger.mark_in_flight(effect_id, owner="earlier")
    ledger.mark_unknown(effect_id, reason="response lost")
    state.mutate(
        "remote_jobs",
        "op",
        lambda _old: {
            "state": "running",
            "remote_id": "job-1",
            "backend_id": "backend-a",
            "task_type": "sglang-trace-analyze",
            "request_sha256": request_sha,
        },
    )
    posts = []

    def service(request):
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            posts.append(request)
            return httpx.Response(500, json={"error": "should not POST"})
        return _completed_poll(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        result = await _adapter(client, state).analyze_trace(repository="repo", objective="trace")
    assert result.task_id == "job-1"
    assert posts == []
    record = EffectLedger(state).get(effect_id)
    assert record["status"] == EffectStatus.CONFIRMED.value
    assert record["result"]["remote_id"] == "job-1"
