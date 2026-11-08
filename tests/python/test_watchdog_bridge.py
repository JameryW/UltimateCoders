"""The stall staircase belongs to the gateway: the worker must ask, then obey.

These tests pin the two halves of that claim:

* `watchdog_bridge.consult_watchdog` speaks exactly one op, sends the detector's
  evidence, fails open on transport trouble, and refuses a *malformed* verdict
  instead of treating it as "keep going";
* `Orchestrator._maybe_stall_agent_redispatch_governed` stops retrying on
  `escalate` / `stop` / `replan`, honours the remaining-attempt budget on
  `continue`, records the verdict on the stall signal, and falls back to exactly
  the pre-existing local policy when the bridge is absent or unreachable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.watchdog_bridge import (
    OP_WATCHDOG_STALL_REPORT,
    consult_watchdog,
    stopping_action,
)
from ultimate_coders.proactive.watchdog_input import WatchdogContractError


class FakeClient:
    """Records ops and answers from a per-op table (or raises one error)."""

    def __init__(self, reply: Any = None, *, error: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._reply = reply
        self._error = error

    async def call(self, op: str, payload: dict[str, Any]) -> Any:
        self.calls.append((op, payload))
        if self._error is not None:
            raise self._error
        return self._reply


def verdict(token: str, *, attempts: int = 1, replans: int = 1, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "verdict": token,
        "replans_remaining": replans,
        "attempts_remaining": attempts,
    }
    payload.update(extra)
    return payload


def stall_signal() -> dict[str, Any]:
    return {
        "loop_detected": True,
        "stalled_streak": 3,
        "reason": "no new evidence",
        "error_signature": "sig-abc",
    }


def result() -> SimpleNamespace:
    return SimpleNamespace(
        stall_signal=stall_signal(),
        retryable=True,
        error="tests failed",
        retry_count=1,
    )


def task() -> SimpleNamespace:
    return SimpleNamespace(id="task-1", case_id="case-1")


def subtask() -> SimpleNamespace:
    return SimpleNamespace(id="subtask-1", agent_config={})


# ── consult_watchdog ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_client_means_no_question() -> None:
    assert await consult_watchdog(None, task_id="task-1") is None


@pytest.mark.asyncio
async def test_the_question_carries_the_detector_evidence() -> None:
    client = FakeClient(verdict("retry_same_contract"))
    action = await consult_watchdog(
        client,
        task_id="task-1",
        subtask_id="subtask-1",
        no_progress_count=3,
        evidence_signature="sig-abc",
        last_error="tests failed",
        confirmed_effect_ids=("push:1",),
        attempts_used=2,
        replans_used=0,
        already_retried=True,
        case_id="case-1",
    )
    assert action is not None
    assert action.action == "retry_same_contract"
    assert stopping_action(action) is False
    op, payload = client.calls[0]
    assert op == OP_WATCHDOG_STALL_REPORT
    assert payload == {
        "task_id": "task-1",
        "subtask_id": "subtask-1",
        "no_progress_count": 3,
        "evidence_signature": "sig-abc",
        "last_error": "tests failed",
        "confirmed_effect_ids": ["push:1"],
        "attempts_used": 2,
        "replans_used": 0,
        "already_retried": True,
        "case_id": "case-1",
    }


@pytest.mark.asyncio
async def test_optional_fields_are_omitted_rather_than_sent_as_null() -> None:
    client = FakeClient(verdict("continue"))
    await consult_watchdog(client, task_id="task-1")
    _, payload = client.calls[0]
    assert "subtask_id" not in payload
    assert "case_id" not in payload
    assert "evidence_signature" not in payload
    assert "last_error" not in payload


@pytest.mark.asyncio
async def test_a_transport_failure_fails_open() -> None:
    client = FakeClient(error=RuntimeError("bridge down"))
    assert await consult_watchdog(client, task_id="task-1") is None


@pytest.mark.asyncio
async def test_an_unknown_verdict_is_rejected_not_obeyed() -> None:
    client = FakeClient(
        {"verdict": "keep_going_forever", "replans_remaining": 0, "attempts_remaining": 0}
    )
    with pytest.raises(WatchdogContractError):
        await consult_watchdog(client, task_id="task-1")


@pytest.mark.asyncio
async def test_a_replan_that_drops_a_confirmed_effect_is_refused() -> None:
    # The gateway said replan but did not preserve the effect this worker already
    # confirmed: obeying it could repeat a push.
    client = FakeClient(verdict("replan", attempts=2, attempt=1, preserve_effects=[]))
    with pytest.raises(WatchdogContractError):
        await consult_watchdog(client, task_id="task-1", confirmed_effect_ids=("push:1",))


@pytest.mark.asyncio
async def test_a_verdict_envelope_is_accepted() -> None:
    client = FakeClient({"stall": verdict("escalate_needs_human", attempts=0, replans=0)})
    action = await consult_watchdog(client, task_id="task-1")
    assert action is not None
    assert action.action == "escalate"
    assert stopping_action(action) is True


# ── Orchestrator wiring ──────────────────────────────────────


@pytest.mark.asyncio
async def test_escalation_stops_the_worker_and_is_recorded_on_the_signal() -> None:
    client = FakeClient(
        verdict("escalate_needs_human", attempts=0, replans=0, reason="stalled 3x")
    )
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()

    redispatched = await orchestrator._maybe_stall_agent_redispatch_governed(
        task(), subtask(), outcome
    )

    assert redispatched is False
    assert outcome.retryable is False
    assert outcome.stall_signal["verdict"] == "escalate"
    assert outcome.stall_signal["verdict_source"] == "gateway"
    assert outcome.stall_signal["stalled_streak"] == 3, "existing evidence is kept"


@pytest.mark.asyncio
async def test_a_replan_is_left_to_the_gateway() -> None:
    # Replanning is the gateway's decision surface: the worker stops and reports
    # the verdict instead of inventing a new plan.
    client = FakeClient(verdict("replan", attempts=2, attempt=1, preserve_effects=["push:1"]))
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    assert outcome.retryable is False
    assert outcome.stall_signal["verdict"] == "replan"


@pytest.mark.asyncio
async def test_no_attempts_left_stops_even_on_continue() -> None:
    client = FakeClient(verdict("continue", attempts=0, replans=0))
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    assert outcome.retryable is False


@pytest.mark.asyncio
async def test_continue_with_budget_defers_to_the_local_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(verdict("retry_same_contract", attempts=2, replans=1))
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()

    calls: list[str] = []

    def fake_local(st: Any, res: Any) -> bool:
        calls.append(st.id)
        return True

    monkeypatch.setattr(orchestrator, "_maybe_stall_agent_redispatch", fake_local)

    redispatched = await orchestrator._maybe_stall_agent_redispatch_governed(
        task(), subtask(), outcome
    )

    assert redispatched is True
    assert calls == ["subtask-1"], "the existing local policy still decides the swap"
    assert outcome.stall_signal["verdict"] == "retry_same_contract"


@pytest.mark.asyncio
async def test_an_unreachable_bridge_keeps_the_old_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(error=RuntimeError("bridge down"))
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()

    calls: list[str] = []

    def fake_local(st: Any, res: Any) -> bool:
        calls.append(st.id)
        return True

    monkeypatch.setattr(orchestrator, "_maybe_stall_agent_redispatch", fake_local)

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is True
    )
    assert calls == ["subtask-1"]
    assert "verdict" not in outcome.stall_signal, "no verdict was received, so none is claimed"


@pytest.mark.asyncio
async def test_without_a_client_the_old_path_runs_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator()
    outcome = result()
    calls: list[str] = []

    def fake_local(st: Any, res: Any) -> bool:
        calls.append(st.id)
        return False

    monkeypatch.setattr(orchestrator, "_maybe_stall_agent_redispatch", fake_local)

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    assert calls == ["subtask-1"]
    assert outcome.retryable is True, "the local policy owns retryability"
    assert "verdict" not in outcome.stall_signal


@pytest.mark.asyncio
async def test_a_malformed_verdict_stops_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient({"verdict": "???", "replans_remaining": 9, "attempts_remaining": 9})
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()

    called: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_maybe_stall_agent_redispatch",
        lambda st, res: called.append(st.id) or True,
    )

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    assert outcome.retryable is False
    assert called == [], "a malformed verdict must not fall through to a retry"


@pytest.mark.asyncio
async def test_a_result_without_a_stall_signal_is_not_questioned() -> None:
    # Non-stall failures keep their existing retry handling; the watchdog is not
    # a general failure oracle.
    client = FakeClient(verdict("escalate_needs_human"))
    orchestrator = Orchestrator(governance_client=client)
    outcome = result()
    outcome.stall_signal = None

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    assert client.calls == [], "nothing was asked, so nothing was assumed"


# ── confirmed effects on the stall (D18 / P8) ───────────────────


@pytest.mark.asyncio
async def test_a_governed_stall_sends_the_attempts_confirmed_effects() -> None:
    client = FakeClient(verdict("escalate_needs_human", attempts=0, replans=0))
    orchestrator = Orchestrator(governance_client=client)
    seen: list[tuple[str, str]] = []

    async def provider(t: Any, st: Any, res: Any) -> list[str]:
        seen.append((t.id, st.id))
        return ["eff_b", "eff_a", "eff_b", "  "]

    orchestrator.confirmed_effects_provider = provider
    outcome = result()

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    assert seen == [("task-1", "subtask-1")]
    payload = client.calls[0][1]
    assert payload["confirmed_effect_ids"] == ["eff_b", "eff_a"], (
        "blank ids dropped, order kept, duplicates collapsed"
    )


@pytest.mark.asyncio
async def test_without_a_provider_the_stall_sends_no_effects() -> None:
    client = FakeClient(verdict("escalate_needs_human", attempts=0, replans=0))
    orchestrator = Orchestrator(governance_client=client)

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), result())
        is False
    )
    assert client.calls[0][1]["confirmed_effect_ids"] == []


@pytest.mark.asyncio
async def test_a_failing_effect_provider_does_not_block_the_stall() -> None:
    client = FakeClient(verdict("escalate_needs_human", attempts=0, replans=0))
    orchestrator = Orchestrator(governance_client=client)

    async def provider(t: Any, st: Any, res: Any) -> list[str]:
        raise RuntimeError("ledger unavailable")

    orchestrator.confirmed_effects_provider = provider
    outcome = result()

    assert (
        await orchestrator._maybe_stall_agent_redispatch_governed(task(), subtask(), outcome)
        is False
    )
    assert client.calls[0][1]["confirmed_effect_ids"] == []
    assert outcome.stall_signal["verdict"] == "escalate"
