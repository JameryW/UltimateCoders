"""Operator reconciliation settles the MetaInfer ledger receipts (T29 / D16).

The operator path proves remote state and moves ``remote_jobs``; the ledger receipt
for the same operation must settle too, or ``EffectLedger.unknown()`` keeps listing a
reconciled effect and a later kill retry is refused as "outcome unknown".
"""

from __future__ import annotations

import hashlib

import pytest
from ultimate_coders.effect_ledger import EffectLedger, EffectStatus, stable_effect_id
from ultimate_coders.inference import adapter as adapter_module
from ultimate_coders.inference.adapter import settle_metainfer_effects_from_operator_evidence
from ultimate_coders.inference.reconcile import RemoteJobs
from ultimate_coders.runtime_state import RuntimeState

_BACKEND = "metainfer:test"
_OPERATION = "op-1"
_REQUEST_SHA = hashlib.sha256(b"request").hexdigest()
_REMOTE_ID = "remote-1"


@pytest.fixture
def state(tmp_path, monkeypatch) -> RuntimeState:
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    return RuntimeState()


def _ledger_row(state: RuntimeState, kind: str, parts: tuple, status: EffectStatus) -> str:
    ledger = EffectLedger(state)
    effect_id = stable_effect_id(kind, *parts)
    ledger.prepare(effect_id, kind=kind, request={"parts": list(parts)}, idempotency_key=effect_id)
    if status is EffectStatus.IN_FLIGHT:
        ledger.mark_in_flight(effect_id, owner="worker:w1")
    elif status is EffectStatus.UNKNOWN:
        ledger.mark_in_flight(effect_id, owner="worker:w1")
        ledger.mark_unknown(effect_id, reason="transport dropped")
    elif status is EffectStatus.FAILED:
        ledger.mark_in_flight(effect_id, owner="worker:w1")
        ledger.fail(effect_id, error="service refused")
    return effect_id


def _status(state: RuntimeState, effect_id: str) -> str:
    return EffectLedger(state).get(effect_id)["status"]


def test_settles_an_unknown_create_receipt(state):
    effect_id = _ledger_row(
        state,
        "metainfer-create",
        (_BACKEND, _OPERATION, _REQUEST_SHA),
        EffectStatus.UNKNOWN,
    )
    settled = settle_metainfer_effects_from_operator_evidence(
        state,
        backend_id=_BACKEND,
        operation_id=_OPERATION,
        request_sha=_REQUEST_SHA,
        remote_id=_REMOTE_ID,
        evidence="operator attach by ops:alice",
    )
    assert settled == [effect_id]
    record = EffectLedger(state).get(effect_id)
    assert record["status"] == EffectStatus.CONFIRMED.value
    assert record["result"]["remote_id"] == _REMOTE_ID
    assert record["result"]["operator_evidence"] == "operator attach by ops:alice"


def test_settles_an_in_flight_kill_receipt(state):
    effect_id = _ledger_row(
        state,
        "metainfer-kill",
        (_BACKEND, _OPERATION, _REMOTE_ID),
        EffectStatus.IN_FLIGHT,
    )
    settled = settle_metainfer_effects_from_operator_evidence(
        state,
        backend_id=_BACKEND,
        operation_id=_OPERATION,
        remote_id=_REMOTE_ID,
        evidence="operator confirm_stop by ops:alice",
    )
    assert settled == [effect_id]
    assert _status(state, effect_id) == EffectStatus.CONFIRMED.value


def test_leaves_a_failed_receipt_alone(state):
    effect_id = _ledger_row(
        state,
        "metainfer-kill",
        (_BACKEND, _OPERATION, _REMOTE_ID),
        EffectStatus.FAILED,
    )
    settled = settle_metainfer_effects_from_operator_evidence(
        state,
        backend_id=_BACKEND,
        operation_id=_OPERATION,
        remote_id=_REMOTE_ID,
        evidence="operator confirm_stop by ops:alice",
    )
    assert settled == [], "evidence never fabricates a success over a recorded failure"
    assert _status(state, effect_id) == EffectStatus.FAILED.value


def test_missing_receipts_are_skipped(state):
    settled = settle_metainfer_effects_from_operator_evidence(
        state,
        backend_id=_BACKEND,
        operation_id="never-ran",
        request_sha=_REQUEST_SHA,
        remote_id=_REMOTE_ID,
        evidence="operator attach by ops:alice",
    )
    assert settled == []


@pytest.mark.asyncio
async def test_confirm_stop_settles_the_kill_receipt_through_the_operator_path(
    state, monkeypatch
):
    kill_id = _ledger_row(
        state, "metainfer-kill", (_BACKEND, _OPERATION, _REMOTE_ID), EffectStatus.UNKNOWN
    )
    state.mutate(
        "remote_jobs",
        _OPERATION,
        lambda old: {
            "state": "cleanup_pending",
            "backend_id": _BACKEND,
            "remote_id": _REMOTE_ID,
            "request_sha256": _REQUEST_SHA,
        },
    )
    version = state.versioned_get("remote_jobs", _OPERATION).version

    async def fake_confirm_quiescence(self, client, remote_id):
        return {"quiesced": True, "remote_id": remote_id}

    monkeypatch.setattr(
        adapter_module.MetaInferAdapter, "confirm_quiescence", fake_confirm_quiescence
    )

    result = await RemoteJobs(state, "http://metainfer.test").reconcile(
        _OPERATION, version, "confirm_stop", actor="ops:alice", client=object()
    )

    assert result["state"] == "stopped"
    assert kill_id in result["settled_effects"]
    assert _status(state, kill_id) == EffectStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_settling_one_backend_leaves_another_backends_receipt_alone(state):
    """The backend id is part of the effect identity.

    Evidence for one backend is not evidence for another.
    """
    mine = _ledger_row(
        state, "metainfer-kill", (_BACKEND, _OPERATION, _REMOTE_ID), EffectStatus.UNKNOWN
    )
    other = _ledger_row(
        state, "metainfer-kill", ("metainfer:other", _OPERATION, _REMOTE_ID), EffectStatus.UNKNOWN
    )

    settled = settle_metainfer_effects_from_operator_evidence(
        state,
        backend_id=_BACKEND,
        operation_id=_OPERATION,
        remote_id=_REMOTE_ID,
        evidence="operator confirm_stop by ops:alice",
    )

    assert settled == [mine]
    assert _status(state, mine) == EffectStatus.CONFIRMED.value
    assert _status(state, other) == EffectStatus.UNKNOWN.value


@pytest.mark.asyncio
async def test_a_confirmed_receipt_is_not_reported_twice(state):
    effect_id = _ledger_row(
        state, "metainfer-kill", (_BACKEND, _OPERATION, _REMOTE_ID), EffectStatus.UNKNOWN
    )
    settle_metainfer_effects_from_operator_evidence(
        state,
        backend_id=_BACKEND,
        operation_id=_OPERATION,
        remote_id=_REMOTE_ID,
        evidence="operator confirm_stop by ops:alice",
    )
    again = settle_metainfer_effects_from_operator_evidence(
        state,
        backend_id=_BACKEND,
        operation_id=_OPERATION,
        remote_id=_REMOTE_ID,
        evidence="operator confirm_stop by ops:alice",
    )
    assert again == [], "an already-settled receipt is not a new settlement"
    assert _status(state, effect_id) == EffectStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_a_legacy_job_without_a_backend_id_uses_the_configured_identity(
    state, monkeypatch
):
    """The adapter's resolution order is env -> url hash; the settle path must match it.

    A worker configured with `UC_METAINFER_BACKEND_ID` writes its receipts under that
    id. If the job record predates the field, the operator path used to fall straight
    to the URL hash and settle nothing -- with an empty `settled_effects` and no warning.
    """
    monkeypatch.setenv("UC_METAINFER_BACKEND_ID", "metainfer:env")
    effect_id = _ledger_row(
        state, "metainfer-kill", ("metainfer:env", _OPERATION, _REMOTE_ID), EffectStatus.UNKNOWN
    )
    state.mutate(
        "remote_jobs",
        _OPERATION,
        lambda old: {
            "state": "cleanup_pending",
            "remote_id": _REMOTE_ID,
            "request_sha256": _REQUEST_SHA,
        },
    )
    version = state.versioned_get("remote_jobs", _OPERATION).version

    async def fake_confirm_quiescence(self, client, remote_id):
        return {"quiesced": True, "remote_id": remote_id}

    monkeypatch.setattr(
        adapter_module.MetaInferAdapter, "confirm_quiescence", fake_confirm_quiescence
    )

    result = await RemoteJobs(state, "http://metainfer.test").reconcile(
        _OPERATION, version, "confirm_stop", actor="ops:alice", client=object()
    )

    assert effect_id in result["settled_effects"]
    assert _status(state, effect_id) == EffectStatus.CONFIRMED.value
