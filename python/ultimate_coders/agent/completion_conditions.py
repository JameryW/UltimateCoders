"""Structured completion conditions: machine-checkable "done", separate from digest.

``contract_digest`` / ``ContractRegistry`` answer only whether goal text changed.
This module answers whether structured completion conditions are satisfied.

Evaluation is pure non-LLM code. Model self-report keys (``approved``, ``done``,
``llm_done``, etc.) alone cannot pass. Product callers gate on the verdict only
when ``UC_COMPLETION_CONDITIONS_ENFORCE`` is on (default off); use
:func:`completion_gate` for that check. :func:`evaluate_completion_conditions`
always computes so tests and tooling can inspect results with the flag off.

Built-in types in this ticket:

- ``evidence_flag`` — ``params={"name": "x"}`` passes iff ``evidence.get("x") is True``
- ``model_approved`` / ``llm_done`` / ``approved`` / ``done`` — always fail (self-report)

Unknown types fail closed. Empty condition lists are satisfied (nothing to check).
Adapters (verify_command, step_condition, independent_reeval) are ticket 06.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

COMPLETION_CONDITIONS_ENFORCE_ENV = "UC_COMPLETION_CONDITIONS_ENFORCE"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

#: Condition types that encode model self-report; they never pass alone.
_SELF_REPORT_TYPES = frozenset({"model_approved", "llm_done", "approved", "done"})


@dataclass(frozen=True)
class CompletionCondition:
    """One machine-checkable completion requirement."""

    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConditionResult:
    """Outcome of evaluating one :class:`CompletionCondition`."""

    type: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class CompletionVerdict:
    """Aggregate outcome: satisfied iff every condition passed (or none given)."""

    satisfied: bool
    results: list[ConditionResult]


def completion_conditions_enforce_enabled() -> bool:
    """Whether product callers should treat evaluate as a hard gate; default off."""
    raw = os.environ.get(COMPLETION_CONDITIONS_ENFORCE_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV


def _coerce_condition(item: CompletionCondition | Mapping[str, Any]) -> CompletionCondition:
    if isinstance(item, CompletionCondition):
        return item
    if not isinstance(item, Mapping):
        raise TypeError(f"condition must be CompletionCondition or mapping, got {type(item)!r}")
    ctype = str(item.get("type", "") or "")
    params = item.get("params") or {}
    if not isinstance(params, Mapping):
        raise TypeError("condition params must be a mapping")
    return CompletionCondition(type=ctype, params=dict(params))


def _eval_one(condition: CompletionCondition, evidence: Mapping[str, Any]) -> ConditionResult:
    ctype = condition.type.strip() if condition.type else ""
    if not ctype:
        return ConditionResult(type=ctype or "", passed=False, reason="unknown condition type ''")

    if ctype in _SELF_REPORT_TYPES:
        return ConditionResult(
            type=ctype,
            passed=False,
            reason=f"model self-report type {ctype!r} cannot alone satisfy completion",
        )

    if ctype == "evidence_flag":
        name = condition.params.get("name")
        if not isinstance(name, str) or not name:
            return ConditionResult(
                type=ctype,
                passed=False,
                reason="evidence_flag requires params['name'] as a non-empty string",
            )
        if evidence.get(name) is True:
            return ConditionResult(
                type=ctype,
                passed=True,
                reason=f"evidence[{name!r}] is True",
            )
        return ConditionResult(
            type=ctype,
            passed=False,
            reason=f"evidence[{name!r}] is not True (got {evidence.get(name)!r})",
        )

    return ConditionResult(
        type=ctype,
        passed=False,
        reason=f"unknown condition type {ctype!r}",
    )


def evaluate_completion_conditions(
    conditions: Sequence[CompletionCondition | Mapping[str, Any]],
    evidence: Mapping[str, Any],
) -> CompletionVerdict:
    """Evaluate structured completion conditions against evidence (pure, non-LLM).

    Empty ``conditions`` → satisfied. Non-empty → all must pass. Unknown types
    and self-report types fail closed. Always computes; does not consult
    ``UC_COMPLETION_CONDITIONS_ENFORCE`` — product gates use :func:`completion_gate`.
    """
    if not conditions:
        return CompletionVerdict(satisfied=True, results=[])

    results = [_eval_one(_coerce_condition(item), evidence) for item in conditions]
    return CompletionVerdict(satisfied=all(r.passed for r in results), results=results)


def completion_gate(
    conditions: Sequence[CompletionCondition | Mapping[str, Any]],
    evidence: Mapping[str, Any],
) -> CompletionVerdict:
    """Product gate: when enforce flag is off, return satisfied without gating.

    When ``UC_COMPLETION_CONDITIONS_ENFORCE`` is on, delegates to
    :func:`evaluate_completion_conditions`.
    """
    if not completion_conditions_enforce_enabled():
        return CompletionVerdict(satisfied=True, results=[])
    return evaluate_completion_conditions(conditions, evidence)
