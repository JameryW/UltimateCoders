"""Ask the gateway watchdog what to do about a stall, and obey the answer.

The progress detector already knows *that* a subtask stopped moving
(`ProgressTracker` -> `stall_signal`). Two things used to happen next: the worker
stopped retrying, and the orchestrator decided locally whether to swap the agent
once. The bounded staircase itself -- how many attempts, how many replans, when a
human has to look -- lives in the gateway (`uc_engine::governance::Watchdog`), and
until now nothing asked it.

This module is that question. It is deliberately thin:

* **One op**, `watchdog.stall.report`, carrying the detector's evidence.
* **The gateway decides**; this module only translates the verdict with
  `apply_verdict`, which enforces that a `replan` may not drop a confirmed
  external effect. When `UC_REPLAN_ACCOUNTING_ENFORCE` is on, prefer the
  authoritative `replans_used` / `replans_remaining` on the reply over any
  local shadow.
* **Fail-open, not fail-silent.** No client, a transport error, or an unreadable
  verdict returns ``None`` (or raises `WatchdogContractError` for a *malformed*
  verdict) so the caller can fall back to the pre-existing local policy. A bridge
  outage must not freeze recovery; it also must not be mistaken for a verdict.
* **No planner here.** On `replan` this module does not invent a new plan: the
  worker stops retrying and reports the verdict, because a replan is the
  gateway's decision surface. Anything else would be a second planner.

Escalation is already projected: the gateway raises the `needs_human` attention
item server-side (`proactive_service::escalate_to_attention`), so a worker that
obeys `escalate` simply stops.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from ultimate_coders.proactive.watchdog_input import (
    WatchdogAction,
    WatchdogContractError,
    apply_verdict,
)

logger = logging.getLogger(__name__)

#: Governance op carrying the stall verdict.
OP_WATCHDOG_STALL_REPORT = "watchdog.stall.report"

#: Actions after which this worker must not retry on its own.
STOPPING_ACTIONS = ("escalate", "stop", "replan")


class WatchdogBridgeError(RuntimeError):
    """The watchdog could not be reached; the caller falls back to local policy."""


async def consult_watchdog(
    client: Any,
    *,
    task_id: str,
    subtask_id: str | None = None,
    no_progress_count: int = 0,
    evidence_signature: str | None = None,
    last_error: str | None = None,
    confirmed_effect_ids: Iterable[str] = (),
    attempts_used: int = 0,
    replans_used: int = 0,
    already_retried: bool = False,
    case_id: str | None = None,
) -> Optional[WatchdogAction]:  # noqa: UP045 - py3.9 runtime
    """Ask the gateway what to do about one stall.

    Returns ``None`` when no client is attached or the gateway cannot be reached —
    the caller then applies the pre-existing local policy. Raises
    :class:`WatchdogContractError` for a verdict that *is* reachable but violates
    the contract: treating a malformed answer as "continue" would let it run
    forever, which is the failure mode the staircase exists to prevent.
    """
    if client is None:
        return None
    payload: dict[str, Any] = {
        "task_id": task_id,
        "no_progress_count": max(0, int(no_progress_count)),
        "confirmed_effect_ids": list(confirmed_effect_ids),
        "attempts_used": max(0, int(attempts_used)),
        "replans_used": max(0, int(replans_used)),
        "already_retried": bool(already_retried),
    }
    if subtask_id:
        payload["subtask_id"] = subtask_id
    if evidence_signature:
        payload["evidence_signature"] = evidence_signature
    if last_error:
        payload["last_error"] = last_error
    if case_id:
        payload["case_id"] = case_id

    try:
        reply = await client.call(OP_WATCHDOG_STALL_REPORT, payload)
    except Exception as exc:  # noqa: BLE001 - every transport failure is "unavailable"
        logger.warning(
            "Watchdog unreachable (%s); falling back to the local stall policy for task %s",
            exc,
            task_id[:8] if task_id else "?",
        )
        return None

    if not isinstance(reply, Mapping):
        raise WatchdogContractError(
            f"watchdog reply must be an object, got {type(reply).__name__}"
        )
    # The bridge returns the verdict view flat; accept a `{"verdict": {...}}`
    # envelope too so one gateway change does not silently disable the staircase.
    if "verdict" not in reply and isinstance(reply.get("stall"), Mapping):
        reply = reply["stall"]
    action = apply_verdict(reply, confirmed_effect_ids=tuple(confirmed_effect_ids))
    logger.info(
        "Watchdog verdict for task %s subtask %s: %s (attempts_left=%d, replans_left=%d)",
        task_id[:8] if task_id else "?",
        (subtask_id or "?")[:8],
        action.action,
        action.attempts_remaining,
        action.replans_remaining,
    )
    return action


def stopping_action(action: WatchdogAction) -> bool:
    """Whether this verdict forbids the worker from retrying on its own."""
    return action.action in STOPPING_ACTIONS


__all__ = [
    "OP_WATCHDOG_STALL_REPORT",
    "STOPPING_ACTIONS",
    "WatchdogBridgeError",
    "consult_watchdog",
    "stopping_action",
]
