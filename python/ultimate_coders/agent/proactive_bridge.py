"""Turn a stopped stall into a governed proposal — from inside the existing loop.

The evaluator was a library with no caller: `ProactiveEvaluator.evaluate` could
produce evidence-backed suggestions, and nothing ever asked. This module is the
caller, at the same seam the watchdog uses -- after the staircase has decided that
a worker must stop.

## What it does, and what it refuses to do

* **One evaluation per stopped stall.** The context is built from facts the worker
  already holds: task, case, project, the detector's stall streak and subtask.
  Nothing is invented, and nothing is fetched.
* **Mode-gated at the source.** `UC_PROACTIVE_MODE=off` (the default) means the
  evaluator is not even called. `observe` evaluates and logs the candidates but
  submits nothing -- observing must not create worklist items.
* **The gateway stays the authority.** Submissions go through
  `proactive.proposal.submit`, which re-checks mode, evidence and budget. A
  refusal (missing permission, empty evidence, mode too low, too many open
  proposals) is *recorded*, never raised: the stall path is already a failure
  path, and a governance refusal is an expected answer there, not an error.
* **No execution.** A proposal is a suggestion; approving it and executing it are
  other people's decisions (`DecideProposal`, `ProactiveGate::admit_execution`).
* **No client, no submission.** Without a bridge this returns immediately; the
  worker keeps its pre-PR06 behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from ultimate_coders.proactive.evaluator import EvaluationResult, ProactiveEvaluator
from ultimate_coders.proactive.modes import (
    ProactiveMode,
    parse_proactive_mode,
    proactive_mode_from_env,
)

logger = logging.getLogger(__name__)

#: Governance op carrying one proposal.
OP_PROPOSAL_SUBMIT = "proactive.proposal.submit"


@dataclass
class ProactiveOutcome:
    """What one stopped stall produced.

    ``observed`` is non-empty only in `observe` mode (evaluated, not surfaced);
    ``submitted`` holds the gateway's answers for the proposals that were sent.
    """

    mode: ProactiveMode
    evaluated: bool = False
    observed: list[dict[str, Any]] = field(default_factory=list)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    blocked_by: Optional[str] = None  # noqa: UP045 - py3.9 runtime

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": self.mode.value,
            "evaluated": self.evaluated,
            "observed": self.observed,
            "submitted": self.submitted,
            "refusals": self.refusals,
        }
        if self.blocked_by:
            payload["blocked_by"] = self.blocked_by
        return payload


def evaluation_context(
    *,
    task_id: str,
    task_status: str | None = None,
    project_id: str | None = None,
    case_id: str | None = None,
    stalled_streak: int = 0,
    action: str = "stop",
    subtask_id: str | None = None,
    budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The evaluator's context, built only from facts the worker already has."""
    context: dict[str, Any] = {
        "task": {"id": task_id},
        "progress": {"action": action, "stalled_streak": max(0, int(stalled_streak))},
    }
    if task_status:
        context["task"]["status"] = task_status
    if project_id:
        context["task"]["project_id"] = project_id
    if case_id:
        context["case"] = {"id": case_id}
    if subtask_id:
        context["progress"]["subtask_id"] = subtask_id
    if budget is not None:
        context["budget"] = dict(budget)
    return context


async def propose_on_stall(
    client: Any,
    *,
    task_id: str,
    task_status: str | None = None,
    project_id: str | None = None,
    case_id: str | None = None,
    stalled_streak: int = 0,
    action: str = "stop",
    subtask_id: str | None = None,
    budget: Mapping[str, Any] | None = None,
    mode: ProactiveMode | None = None,
    evaluator: ProactiveEvaluator | None = None,
    principal_kind: str = "agent",
    principal_id: str = "uc-worker",
) -> ProactiveOutcome:
    """Evaluate a stopped stall and submit what the mode allows.

    Returns the outcome rather than raising: the caller is on a failure path and
    must not fail again because a suggestion could not be delivered.
    """
    active = mode if mode is not None else proactive_mode_from_env()
    outcome = ProactiveOutcome(mode=active)
    if not active.evaluates():
        # `off` is the default: do not even build the context.
        return outcome
    if client is None:
        logger.debug("proactive evaluation skipped: no governance bridge attached")
        return outcome

    engine = evaluator if evaluator is not None else ProactiveEvaluator()
    context = evaluation_context(
        task_id=task_id,
        task_status=task_status,
        project_id=project_id,
        case_id=case_id,
        stalled_streak=stalled_streak,
        action=action,
        subtask_id=subtask_id,
        budget=budget,
    )
    try:
        result: EvaluationResult = engine.evaluate(context, mode=active)
    except Exception:  # noqa: BLE001 - a broken evaluator must not break recovery
        logger.warning(
            "proactive evaluation failed for task %s",
            task_id[:8] if task_id else "?",
            exc_info=True,
        )
        return outcome

    outcome.evaluated = result.evaluated
    outcome.blocked_by = result.blocked_by
    outcome.refusals = [refusal.to_dict() for refusal in result.refusals]
    if result.observed:
        outcome.observed = [proposal.to_dict() for proposal in result.observed]
        logger.info(
            "proactive (%s): %d candidate(s) observed for task %s, not submitted",
            active.value,
            len(outcome.observed),
            task_id[:8] if task_id else "?",
        )
    if result.blocked_by:
        logger.info(
            "proactive evaluation blocked by %s for task %s",
            result.blocked_by,
            task_id[:8] if task_id else "?",
        )
    if not result.proposals:
        return outcome

    for proposal in result.proposals:
        payload = proposal.to_submit_payload(
            principal_kind=principal_kind, principal_id=principal_id
        )
        try:
            reply = await client.call(OP_PROPOSAL_SUBMIT, payload)
        except Exception as exc:  # noqa: BLE001 - refusal/unavailable are both "not submitted"
            code = getattr(exc, "code", None) or type(exc).__name__
            logger.info(
                "proactive proposal %s not submitted for task %s (%s)",
                proposal.kind.value,
                task_id[:8] if task_id else "?",
                code,
            )
            outcome.submitted.append(
                {
                    "kind": proposal.kind.value,
                    "title": proposal.title,
                    "accepted": False,
                    "code": str(code),
                }
            )
            continue
        accepted = bool(reply.get("success", True)) if isinstance(reply, Mapping) else True
        proposed_id = None
        if isinstance(reply, Mapping):
            record = reply.get("proposal")
            if isinstance(record, Mapping):
                proposed_id = record.get("id")
        outcome.submitted.append(
            {
                "kind": proposal.kind.value,
                "title": proposal.title,
                "accepted": accepted,
                "proposal_id": proposed_id,
            }
        )
        logger.info(
            "proactive proposal %s submitted for task %s (id=%s, accepted=%s)",
            proposal.kind.value,
            task_id[:8] if task_id else "?",
            proposed_id or "?",
            accepted,
        )
    return outcome


def mode_from(environ: Mapping[str, str] | None = None) -> ProactiveMode:
    """Mode lookup, tolerant of an unknown value (unknown never enables execution)."""
    return parse_proactive_mode(proactive_mode_from_env(environ).value) or ProactiveMode.OFF


__all__ = [
    "OP_PROPOSAL_SUBMIT",
    "ProactiveOutcome",
    "evaluation_context",
    "mode_from",
    "propose_on_stall",
]
