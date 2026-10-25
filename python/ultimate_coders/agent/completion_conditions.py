"""Structured completion conditions: machine-checkable "done", separate from digest.

``contract_digest`` / ``ContractRegistry`` answer only whether goal text changed.
This module answers whether structured completion conditions are satisfied.

Evaluation is pure non-LLM code. Model self-report keys (``approved``, ``done``,
``llm_done``, etc.) alone cannot pass. Product callers gate on the verdict only
when ``UC_COMPLETION_CONDITIONS_ENFORCE`` is on (default off); use
:func:`completion_gate` for that check. :func:`evaluate_completion_conditions`
always computes so tests and tooling can inspect results with the flag off.

Built-in types:

- ``evidence_flag`` — ``params={"name": "x"}`` passes iff ``evidence.get("x") is True``
- ``verify_command`` — passes iff ``evidence.get("verify_command_passed") is True``
  (caller runs the command elsewhere; optional ``params["cmd"]`` is diagnostics only)
- ``step_condition`` — ``params={"expr": "..."}``; evaluates via
  :mod:`ultimate_coders.agent.step_condition` against ``evidence["prev"]``;
  parse errors fail closed
- ``independent_reeval`` — passes iff sealed re-eval evidence passed
  (``IndependentReevalOutcome.passed`` or ``independent_reeval_passed is True``);
  model/review self-approval alone cannot satisfy
- ``review_approved`` — passes only when ``evidence.get("review_verified") is True``
  (review JSON approval alone never bypasses non-LLM evaluate)
- ``model_approved`` / ``llm_done`` / ``approved`` / ``done`` — always fail (self-report)

Unknown types fail closed. Empty condition lists are satisfied (nothing to check).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ultimate_coders.agent import step_condition as step_cond
from ultimate_coders.inference.benchmark import IndependentReevalOutcome

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


def _eval_verify_command(
    condition: CompletionCondition, evidence: Mapping[str, Any]
) -> ConditionResult:
    ctype = "verify_command"
    cmd = condition.params.get("cmd")
    cmd_note = f" (cmd={cmd!r})" if isinstance(cmd, str) and cmd else ""
    if evidence.get("verify_command_passed") is True:
        return ConditionResult(
            type=ctype,
            passed=True,
            reason=f"evidence['verify_command_passed'] is True{cmd_note}",
        )
    return ConditionResult(
        type=ctype,
        passed=False,
        reason=(
            "evidence['verify_command_passed'] is not True "
            f"(got {evidence.get('verify_command_passed')!r}){cmd_note}"
        ),
    )


def _eval_step_condition(
    condition: CompletionCondition, evidence: Mapping[str, Any]
) -> ConditionResult:
    ctype = "step_condition"
    expr = condition.params.get("expr")
    if not isinstance(expr, str):
        return ConditionResult(
            type=ctype,
            passed=False,
            reason="step_condition requires params['expr'] as a string",
        )
    prev = evidence.get("prev")
    try:
        ok = step_cond.evaluate(expr, prev)
    except step_cond.ConditionError as exc:
        return ConditionResult(
            type=ctype,
            passed=False,
            reason=f"step_condition parse/eval error: {exc}",
        )
    if ok:
        return ConditionResult(
            type=ctype,
            passed=True,
            reason=f"step_condition expr satisfied: {expr!r}",
        )
    return ConditionResult(
        type=ctype,
        passed=False,
        reason=f"step_condition expr not satisfied: {expr!r}",
    )


def _eval_independent_reeval(evidence: Mapping[str, Any]) -> ConditionResult:
    ctype = "independent_reeval"
    outcome = evidence.get("independent_reeval")
    if isinstance(outcome, IndependentReevalOutcome):
        if outcome.passed is True:
            return ConditionResult(
                type=ctype,
                passed=True,
                reason="IndependentReevalOutcome.passed is True",
            )
        return ConditionResult(
            type=ctype,
            passed=False,
            reason=f"IndependentReevalOutcome.passed is not True (got {outcome.passed!r})",
        )
    if evidence.get("independent_reeval_passed") is True:
        return ConditionResult(
            type=ctype,
            passed=True,
            reason="evidence['independent_reeval_passed'] is True",
        )
    return ConditionResult(
        type=ctype,
        passed=False,
        reason=(
            "independent_reeval requires IndependentReevalOutcome.passed or "
            f"independent_reeval_passed is True "
            f"(got outcome={outcome!r}, "
            f"flag={evidence.get('independent_reeval_passed')!r})"
        ),
    )


def _eval_review_approved(evidence: Mapping[str, Any]) -> ConditionResult:
    ctype = "review_approved"
    # Review-node JSON approval alone must not bypass non-LLM evaluate.
    if evidence.get("review_verified") is True:
        return ConditionResult(
            type=ctype,
            passed=True,
            reason="evidence['review_verified'] is True",
        )
    return ConditionResult(
        type=ctype,
        passed=False,
        reason=(
            "review_approved requires evidence['review_verified'] is True "
            f"(got {evidence.get('review_verified')!r}); "
            "review self-approval alone cannot satisfy completion"
        ),
    )


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

    if ctype == "verify_command":
        return _eval_verify_command(condition, evidence)

    if ctype == "step_condition":
        return _eval_step_condition(condition, evidence)

    if ctype == "independent_reeval":
        return _eval_independent_reeval(evidence)

    if ctype == "review_approved":
        return _eval_review_approved(evidence)

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
