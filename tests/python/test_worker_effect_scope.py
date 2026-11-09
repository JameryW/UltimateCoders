"""The worker's confirmed-effect lookup (D18 / D22).

The orchestrator side is covered with an injected fake provider; this covers the real
one. It derives the attempt's scope from what the ledger records already carry -- both
`uc/subtask/<id>` branch forms, the experiment-id prefix on MetaInfer operations, and
(D22) the branches a merge receipt carried -- and returns nothing when a receipt cannot
be scoped.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ultimate_coders.agent.worker import Worker
from ultimate_coders.agent.workspace import subtask_branch
from ultimate_coders.effect_ledger import EffectLedger
from ultimate_coders.runtime_state import RuntimeState

SUBTASK_ID = "subtask-abcdef1234567890"


@pytest.fixture()
def state(tmp_path) -> RuntimeState:
    return RuntimeState(tmp_path / "state.db")


def _worker(state: RuntimeState) -> SimpleNamespace:
    async def domain_state() -> RuntimeState:
        return state

    return SimpleNamespace(_domain_state=domain_state)


def _subtask(subtask_id: str = SUBTASK_ID) -> SimpleNamespace:
    return SimpleNamespace(id=subtask_id)


def _result(experiment: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(domain_result={"experiment_id": experiment} if experiment else {})


async def _lookup(state, *, experiment: str | None = None) -> list[str]:
    return await Worker.confirmed_effect_ids_for(
        _worker(state), None, _subtask(), _result(experiment)
    )


def _confirmed(state: RuntimeState, effect_id: str, kind: str, request: dict) -> None:
    ledger = EffectLedger(state)
    ledger.prepare(effect_id, kind=kind, request=request)
    ledger.mark_in_flight(effect_id, owner="w1")
    ledger.confirm(effect_id)


@pytest.mark.asyncio
async def test_the_coding_branch_scopes_a_push(state):
    _confirmed(
        state, "eff_push", "push-branch", {"branch": subtask_branch(SUBTASK_ID)}
    )
    assert await _lookup(state) == ["eff_push"]


@pytest.mark.asyncio
async def test_the_inference_branch_form_is_scoped_too(state):
    _confirmed(
        state,
        "eff_infer_push",
        "push-branch",
        {"branch": subtask_branch(SUBTASK_ID, inference=True)},
    )
    assert await _lookup(state) == ["eff_infer_push"]


@pytest.mark.asyncio
async def test_the_experiment_prefix_scopes_a_metainfer_effect(state):
    _confirmed(state, "eff_meta", "metainfer-create", {"operation_id": "exp-1:2"})
    assert await _lookup(state, experiment="exp-1") == ["eff_meta"]
    assert await _lookup(state) == [], "no experiment id means no prefix scope"


@pytest.mark.asyncio
async def test_a_merge_receipt_is_scoped_by_the_branches_it_carried(state):
    _confirmed(
        state,
        "eff_merge",
        "push-main",
        {
            "branch": "main",
            "remote": "origin",
            "merged_branches": [subtask_branch(SUBTASK_ID)],
        },
    )
    assert await _lookup(state) == ["eff_merge"]


@pytest.mark.asyncio
async def test_a_branch_less_merge_receipt_stays_out(state):
    _confirmed(state, "eff_merge", "push-main", {"branch": "main", "remote": "origin"})
    assert await _lookup(state) == []


@pytest.mark.asyncio
async def test_unreconciled_effects_are_not_preserved(state):
    ledger = EffectLedger(state)
    ledger.prepare(
        "eff_unknown", kind="push-branch", request={"branch": subtask_branch(SUBTASK_ID)}
    )
    ledger.mark_in_flight("eff_unknown", owner="w1")
    ledger.mark_unknown("eff_unknown", reason="lost response")
    assert await _lookup(state) == []


@pytest.mark.asyncio
async def test_a_lookup_failure_returns_nothing_instead_of_raising():
    async def broken() -> RuntimeState:
        raise RuntimeError("state unavailable")

    ids = await Worker.confirmed_effect_ids_for(
        SimpleNamespace(_domain_state=broken), None, _subtask(), _result()
    )
    assert ids == [], "evidence lookup must never fail the subtask"
