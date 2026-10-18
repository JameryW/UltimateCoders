"""Task-level budget: admit while there is room, refuse and preserve state after.

The ledger's job is to be honest about what it knows. A missing usage report
is "unknown", not zero; a gate checked before execution overshoots by at most
one subtask per in-flight subtask. Both are asserted here rather than hidden.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ultimate_coders.agent.types import Subtask, SubtaskResult, SubtaskUsage
from ultimate_coders.budget import (
    ENV_COST_BUDGET,
    ENV_TIME_BUDGET,
    ENV_TOKEN_BUDGET,
    BudgetLimits,
    TaskBudgetLedger,
)
from ultimate_coders.nats_worker import NatsWorker
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return RuntimeState()


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── limits ─────────────────────────────────────────────────────────


def test_env_limits_parse_and_bad_values_are_ignored(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN_BUDGET, "120000")
    monkeypatch.setenv(ENV_COST_BUDGET, "2.5")
    monkeypatch.setenv(ENV_TIME_BUDGET, "not-a-number")
    limits = BudgetLimits.from_env()
    assert (limits.tokens, limits.cost_usd, limits.seconds) == (120000, 2.5, None)


def test_zero_and_empty_mean_unlimited(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN_BUDGET, "0")
    monkeypatch.setenv(ENV_COST_BUDGET, "")
    monkeypatch.delenv(ENV_TIME_BUDGET, raising=False)
    assert BudgetLimits.from_env().unlimited is True


# ── accounting ─────────────────────────────────────────────────────


def test_first_admission_stamps_the_clock_and_is_within_budget(state):
    clock = FakeClock()
    ledger = TaskBudgetLedger(state, BudgetLimits(tokens=100), clock=clock)
    verdict = ledger.admit("t1")
    assert (verdict.admitted, verdict.reason) == (True, "within_budget")
    assert verdict.elapsed_seconds == 0
    assert state.get("task_budgets", "t1")["started_at"] == clock.now


def test_unlimited_ledger_never_refuses(state):
    ledger = TaskBudgetLedger(state, BudgetLimits(), clock=FakeClock())
    for _ in range(5):
        assert ledger.admit("t1").reason == "unlimited"


def test_usage_accumulates_across_completions(state):
    clock = FakeClock()
    ledger = TaskBudgetLedger(state, BudgetLimits(tokens=100), clock=clock)
    ledger.admit("t1")
    ledger.record("t1", SubtaskUsage(input_tokens=30, output_tokens=10), subtask_id="s1")
    verdict = ledger.record("t1", SubtaskUsage(input_tokens=20, output_tokens=5), subtask_id="s2")
    assert verdict.tokens_used == 65
    assert verdict.executions == 2
    assert verdict.admitted is True  # 65 < 100


def test_cost_sums_exactly_in_micro_dollars(state):
    """0.1 + 0.2 must be 0.3, not 0.30000000000000004.

    The total is persisted, so float drift would compound across restarts and
    make the budget unauditable.
    """
    ledger = TaskBudgetLedger(state, BudgetLimits(cost_usd=1.0), clock=FakeClock())
    for _ in range(3):
        ledger.record("t1", SubtaskUsage(total_cost_usd=0.1))
    assert state.get("task_budgets", "t1")["cost_micro_usd"] == 300_000
    assert ledger.snapshot("t1")["cost_used_usd"] == 0.3


def test_unreported_usage_counts_the_execution_but_not_the_totals(state):
    ledger = TaskBudgetLedger(state, BudgetLimits(tokens=10), clock=FakeClock())
    verdict = ledger.record("t1", None, subtask_id="s1")
    assert (verdict.tokens_used, verdict.executions, verdict.unreported) == (0, 1, 1)
    # A block that names its adapter but reports no number is not a
    # measurement either.
    verdict = ledger.record("t1", SubtaskUsage(source="claude-cli"))
    assert verdict.unreported == 2 and verdict.tokens_used == 0
    assert verdict.admitted is True  # unknown spend must not read as exhausted


def test_partial_usage_counts_what_is_there(state):
    ledger = TaskBudgetLedger(state, BudgetLimits(tokens=100), clock=FakeClock())
    verdict = ledger.record("t1", SubtaskUsage(input_tokens=12))
    assert (verdict.tokens_used, verdict.unreported) == (12, 0)


# ── refusal ────────────────────────────────────────────────────────


def test_token_exhaustion_refuses_with_the_used_numbers(state):
    ledger = TaskBudgetLedger(state, BudgetLimits(tokens=100), clock=FakeClock())
    ledger.record("t1", SubtaskUsage(input_tokens=60, output_tokens=40))
    verdict = ledger.admit("t1")
    assert (verdict.admitted, verdict.reason) == (False, "tokens")
    assert verdict.tokens_used == 100


def test_cost_exhaustion_refuses(state):
    ledger = TaskBudgetLedger(state, BudgetLimits(cost_usd=0.5), clock=FakeClock())
    ledger.record("t1", SubtaskUsage(total_cost_usd=0.75))
    assert ledger.admit("t1").reason == "cost"


def test_time_budget_measures_from_the_first_admission(state):
    clock = FakeClock()
    ledger = TaskBudgetLedger(state, BudgetLimits(seconds=60), clock=clock)
    assert ledger.admit("t1").admitted is True
    clock.advance(59)
    assert ledger.admit("t1").admitted is True
    clock.advance(2)
    verdict = ledger.admit("t1")
    assert (verdict.admitted, verdict.reason) == (False, "time")
    assert verdict.elapsed_seconds == 61


def test_tokens_win_the_reason_when_several_caps_are_met(state):
    clock = FakeClock()
    ledger = TaskBudgetLedger(state, BudgetLimits(tokens=10, cost_usd=0.01, seconds=1), clock=clock)
    ledger.record("t1", SubtaskUsage(input_tokens=10, total_cost_usd=0.02))
    clock.advance(5)
    assert ledger.admit("t1").reason == "tokens"


def test_verdict_wire_shape_is_flat_and_complete(state):
    ledger = TaskBudgetLedger(state, BudgetLimits(tokens=100), clock=FakeClock())
    ledger.record("t1", SubtaskUsage(input_tokens=5))
    payload = ledger.admit("t1").to_dict()
    assert set(payload) == {
        "admitted",
        "reason",
        "limits",
        "tokens_used",
        "cost_used_usd",
        "elapsed_seconds",
        "unreported",
        "executions",
    }


# ── worker gate ────────────────────────────────────────────────────


def _worker(execute, limits: BudgetLimits) -> NatsWorker:
    worker = NatsWorker(mode="worker")
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=execute)
    worker._publisher = SimpleNamespace(publish_terminal=AsyncMock(return_value=True))
    worker._budget_limits_obj = limits
    return worker


@pytest.mark.asyncio
async def test_no_limits_means_no_ledger_writes(state):
    """Unconfigured budgets must cost nothing.

    Admission is a durable read-modify-write; one per subtask would serialize
    concurrent starts against the state store for a gate nobody asked for.
    """
    execute = AsyncMock(return_value=SubtaskResult(subtask_id="node", summary="done", success=True))
    worker = _worker(execute, BudgetLimits())
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph"), js_msg=SimpleNamespace(ack=AsyncMock())
    )
    assert execute.await_count == 1
    assert state.get("task_budgets", "graph") is None


@pytest.mark.asyncio
async def test_exhausted_budget_refuses_before_creating_a_sandbox(state):
    execute = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="second",
            summary="should never run",
            success=True,
            usage=SubtaskUsage(input_tokens=999),
        )
    )
    worker = _worker(execute, BudgetLimits(tokens=50))

    first_execute = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="first",
            summary="done",
            success=True,
            usage=SubtaskUsage(input_tokens=60),
        )
    )
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=first_execute)
    await worker._execute_and_report_body(
        Subtask(id="first", parent_id="graph"), js_msg=SimpleNamespace(ack=AsyncMock())
    )
    assert first_execute.await_count == 1
    assert state.get("task_budgets", "graph")["tokens_used"] == 60

    # Second subtask of the same task: 60 >= 50, so it must not start.
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=execute)
    await worker._execute_and_report_body(
        Subtask(id="second", parent_id="graph"), js_msg=SimpleNamespace(ack=AsyncMock())
    )
    assert execute.await_count == 0
    record = state.get("result_outbox", "graph:second")
    data = record["event"]["data"]
    assert data["budget_exhausted"] is True
    assert data["budget_reason"] == "tokens"
    assert data["budget"]["tokens_used"] == 60
    assert data["retryable"] is False


@pytest.mark.asyncio
async def test_raising_the_limit_lets_a_later_attempt_run(state):
    failing = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="node", summary="flaky", success=False, retryable=True
        )
    )
    worker = _worker(failing, BudgetLimits(tokens=10))
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph"), js_msg=SimpleNamespace(ack=AsyncMock())
    )
    # No usage reported, so nothing was spent: an unreported attempt must not
    # read as a spent budget.
    assert state.get("task_budgets", "graph")["unreported"] == 1
    assert state.get("task_budgets", "graph")["tokens_used"] == 0

    spent = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="node",
            summary="spent",
            success=False,
            retryable=True,
            usage=SubtaskUsage(input_tokens=10),
        )
    )
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=spent)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", dispatch_retry_count=1),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    assert spent.await_count == 1

    blocked = AsyncMock(return_value=SubtaskResult(subtask_id="node", summary="x", success=True))
    worker._worker = SimpleNamespace(worker_id="w1", execute_subtask=blocked)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", dispatch_retry_count=2),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    assert blocked.await_count == 0
    assert state.get("result_outbox", "graph:node")["event"]["data"]["budget_exhausted"] is True

    # An operator raises the cap; the next attempt runs again.
    worker._budget_limits_obj = BudgetLimits(tokens=100)
    await worker._execute_and_report_body(
        Subtask(id="node", parent_id="graph", dispatch_retry_count=3),
        js_msg=SimpleNamespace(ack=AsyncMock()),
    )
    assert blocked.await_count == 1
