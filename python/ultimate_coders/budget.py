"""Task-level budget: admit work while there is budget, stop when there is not.

F7 in ``docs/agent-os-architecture.md``: a task that keeps spending after
its budget is gone is not making progress, it is burning the operator's
money. This module is the accounting half -- a durable per-task ledger of
what has been consumed -- and one admission rule checked before each
subtask starts.

Three measurements, each optional and each enforced independently:

* tokens  -- ``input_tokens + output_tokens`` as reported by the executor
* cost    -- ``total_cost_usd`` as reported by the executor
* time    -- wall clock from the first admission, not from an executor report

What this deliberately does **not** do:

* **It does not invent zeros.** ``SubtaskUsage`` fields are optional and a
  missing number means "unknown", not 0 (the same rule ``types.py`` states
  for the wire format). A completion with no report increments
  ``unreported`` and leaves the counters alone, so every total is a lower
  bound and says so.
* **It does not promise exactness under concurrency.** Admission happens
  before execution, so N subtasks already in flight can each still finish;
  the worst-case overshoot is one subtask's consumption per in-flight
  subtask. This is a budget, not a rate limiter -- the alternative
  (estimating a reservation up front) would gate on a guess.
* **It does not decide what to do instead.** Exhaustion refuses new work
  and preserves state; re-planning or raising the budget is a decision for
  the orchestrator or an operator.

Storage is the existing ``RuntimeState`` namespace ``task_budgets``, so the
ledger survives worker restarts, and every mutation runs inside
``mutate()`` (atomic check-and-set).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from .runtime_state import RuntimeState

logger = logging.getLogger(__name__)

#: State namespace holding one record per task.
BUDGET_NAMESPACE = "task_budgets"

#: Env names read by :meth:`BudgetLimits.from_env`. An unset, empty, zero or
#: unparseable value means "no limit on this dimension" -- a typo must not
#: silently stop a fleet, so a bad value is logged and ignored.
ENV_TOKEN_BUDGET = "UC_TASK_TOKEN_BUDGET"
ENV_COST_BUDGET = "UC_TASK_COST_BUDGET_USD"
ENV_TIME_BUDGET = "UC_TASK_TIME_BUDGET_SECS"


@dataclass(frozen=True)
class BudgetLimits:
    """Caps for one task. ``None`` on a dimension means unlimited."""

    tokens: int | None = None
    cost_usd: float | None = None
    seconds: float | None = None

    @property
    def unlimited(self) -> bool:
        return self.tokens is None and self.cost_usd is None and self.seconds is None

    @classmethod
    def from_env(cls) -> BudgetLimits:
        return cls(
            tokens=_positive_int(os.environ.get(ENV_TOKEN_BUDGET), ENV_TOKEN_BUDGET),
            cost_usd=_positive_float(os.environ.get(ENV_COST_BUDGET), ENV_COST_BUDGET),
            seconds=_positive_float(os.environ.get(ENV_TIME_BUDGET), ENV_TIME_BUDGET),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "cost_usd": self.cost_usd, "seconds": self.seconds}


def _positive_int(raw: str | None, name: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("Budget env %s=%r is not a number; ignoring", name, raw)
        return None
    return value if value > 0 else None


def _positive_float(raw: str | None, name: str) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Budget env %s=%r is not a number; ignoring", name, raw)
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class BudgetVerdict:
    """Outcome of one admission check, plus the numbers behind it."""

    admitted: bool
    #: ``unlimited`` | ``within_budget`` | ``tokens`` | ``cost`` | ``time``
    reason: str
    limits: BudgetLimits
    tokens_used: int = 0
    cost_used_usd: float = 0.0
    elapsed_seconds: float = 0.0
    #: Completions whose usage the executor did not report. Every total above
    #: is a lower bound by this many completions' worth of unknown spend.
    unreported: int = 0
    #: Subtasks that ran under this task (whether or not they reported usage).
    executions: int = 0

    @property
    def exhausted(self) -> bool:
        return not self.admitted

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "limits": self.limits.to_dict(),
            "tokens_used": self.tokens_used,
            "cost_used_usd": self.cost_used_usd,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "unreported": self.unreported,
            "executions": self.executions,
        }


def _verdict(record: dict, limits: BudgetLimits, now: float, *, admitted: bool, reason: str):
    started = record.get("started_at")
    elapsed = max(0.0, now - float(started)) if started else 0.0
    return BudgetVerdict(
        admitted=admitted,
        reason=reason,
        limits=limits,
        tokens_used=int(record.get("tokens_used", 0)),
        cost_used_usd=int(record.get("cost_micro_usd", 0)) / 1_000_000,
        elapsed_seconds=elapsed,
        unreported=int(record.get("unreported", 0)),
        executions=int(record.get("executions", 0)),
    )


def _exhaustion_reason(record: dict, limits: BudgetLimits, now: float) -> str | None:
    """Which cap is already met. ``None`` means there is room to start."""
    if limits.tokens is not None and int(record.get("tokens_used", 0)) >= limits.tokens:
        return "tokens"
    if limits.cost_usd is not None:
        spent = int(record.get("cost_micro_usd", 0)) / 1_000_000
        if spent >= limits.cost_usd:
            return "cost"
    if limits.seconds is not None:
        started = record.get("started_at")
        if started and now - float(started) >= limits.seconds:
            return "time"
    return None


class TaskBudgetLedger:
    """Durable per-task consumption with an admission check.

    ``clock`` is injectable so tests describe time instead of waiting for it.
    """

    def __init__(
        self,
        state: RuntimeState,
        limits: BudgetLimits | None = None,
        *,
        clock: Any = None,
    ) -> None:
        self.state = state
        self.limits = limits if limits is not None else BudgetLimits.from_env()
        self._clock = clock or time.time

    # ── admission ────────────────────────────────────────────────

    def admit(self, task_id: str) -> BudgetVerdict:
        """Whether one more subtask may start now.

        Read-only with respect to consumption: the first admission stamps
        ``started_at`` (which is what the time budget measures from), and
        that is the only write.
        """
        now = self._clock()

        def init(old: dict) -> dict:
            if old:
                return old
            return {
                "task_id": task_id,
                "started_at": now,
                "tokens_used": 0,
                "cost_micro_usd": 0,
                "executions": 0,
                "unreported": 0,
            }

        record = self.state.mutate(BUDGET_NAMESPACE, task_id, init)
        if self.limits.unlimited:
            return _verdict(record, self.limits, now, admitted=True, reason="unlimited")
        reason = _exhaustion_reason(record, self.limits, now)
        if reason is not None:
            return _verdict(record, self.limits, now, admitted=False, reason=reason)
        return _verdict(record, self.limits, now, admitted=True, reason="within_budget")

    # ── accounting ───────────────────────────────────────────────

    def record(
        self,
        task_id: str,
        usage: Any | None,
        *,
        subtask_id: str = "",
    ) -> BudgetVerdict:
        """Add one completion's consumption and return the updated verdict.

        ``usage`` is a ``SubtaskUsage`` (or anything with the same three
        fields). ``None``, or a block with no numbers, counts as *unreported*:
        the execution is counted, the totals are not touched.
        """
        tokens, micro_usd, reported = _measure(usage)
        now = self._clock()

        def apply(old: dict) -> dict:
            base = old or {
                "task_id": task_id,
                "started_at": now,
                "tokens_used": 0,
                "cost_micro_usd": 0,
                "executions": 0,
                "unreported": 0,
            }
            updated = dict(base)
            updated["executions"] = int(base.get("executions", 0)) + 1
            updated["last_subtask_id"] = subtask_id
            updated["updated_at"] = now
            if tokens:
                updated["tokens_used"] = int(base.get("tokens_used", 0)) + tokens
            if micro_usd:
                updated["cost_micro_usd"] = int(base.get("cost_micro_usd", 0)) + micro_usd
            if not reported:
                updated["unreported"] = int(base.get("unreported", 0)) + 1
            return updated

        record = self.state.mutate(BUDGET_NAMESPACE, task_id, apply)
        # A recorded completion does not consume more budget by itself, but
        # reporting whether the cap is now met is what makes the next
        # refusal explainable.
        reason = (
            "unlimited" if self.limits.unlimited else _exhaustion_reason(record, self.limits, now)
        )
        return _verdict(
            record,
            self.limits,
            now,
            admitted=reason is None,
            reason=reason or "within_budget",
        )

    def snapshot(self, task_id: str) -> dict | None:
        record = self.state.get(BUDGET_NAMESPACE, task_id)
        if not record:
            return None
        return {
            "tokens_used": int(record.get("tokens_used", 0)),
            "cost_used_usd": int(record.get("cost_micro_usd", 0)) / 1_000_000,
            "executions": int(record.get("executions", 0)),
            "unreported": int(record.get("unreported", 0)),
            "started_at": record.get("started_at"),
        }


def _measure(usage: Any | None) -> tuple[int, int, bool]:
    """``(tokens, micro_usd, reported)`` for one completion.

    Cost is summed in integer micro-dollars: repeatedly adding floats to a
    persisted total drifts, and a budget that drifts is a budget that cannot
    be audited.
    """
    if usage is None:
        return 0, 0, False
    tokens = 0
    for name in ("input_tokens", "output_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int) and value > 0:
            tokens += value
    cost = getattr(usage, "total_cost_usd", None)
    micro = round(float(cost) * 1_000_000) if isinstance(cost, (int, float)) and cost > 0 else 0
    return tokens, micro, bool(tokens or micro)
