"""Stall signal surface + redispatch enforce flag (P2 / T13 / D7 observe half).

Progress-stop / ``loop_detected`` becomes a first-class structured payload so
orchestrator / NATS consumers can react without scraping logs.

``UC_STALL_REDISPATCH_ENFORCE`` defaults **off**. When off, behaviour matches
today: stop without agent redispatch. Ticket 14 owns the one-shot alternate-
agent swap; this module must not invent replan or agent-swap APIs. The
progress detector (``progress.py``) stays stop/retry evidence only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ultimate_coders.agent.progress import ProgressVerdict

STALL_REDISPATCH_ENFORCE_ENV = "UC_STALL_REDISPATCH_ENFORCE"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class StallSignal:
    """Structured progress-stop signal for events / failure_data payloads."""

    loop_detected: bool
    stalled_streak: int
    reason: str
    error_signature: str
    subtask_id: str | None = None
    contract_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for NATS / orchestrator event payloads.

        Omits optional identity fields when unset so consumers can key on
        presence without filtering nulls.
        """
        payload: dict[str, Any] = {
            "loop_detected": self.loop_detected,
            "stalled_streak": self.stalled_streak,
            "reason": self.reason,
            "error_signature": self.error_signature,
        }
        if self.subtask_id is not None:
            payload["subtask_id"] = self.subtask_id
        if self.contract_digest is not None:
            payload["contract_digest"] = self.contract_digest
        return payload


def stall_redispatch_enforce_enabled() -> bool:
    """Whether stall redispatch enforce is on; default off.

    T13 is observe-only: this flag must not swap agents here. Ticket 14
    consumes the same env when enforce is opted in.
    """
    raw = os.environ.get(STALL_REDISPATCH_ENFORCE_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV


def build_stall_signal(
    verdict: ProgressVerdict,
    *,
    subtask_id: str | None = None,
    contract_digest: str | None = None,
) -> StallSignal | None:
    """Build a :class:`StallSignal` when ``verdict.action == "stop"``.

    Returns ``None`` for retry / non-stop verdicts so callers can attach the
    field only on terminal progress-stop without inventing a replan API.
    """
    if verdict.action != "stop":
        return None
    return StallSignal(
        loop_detected=True,
        stalled_streak=verdict.stalled_streak,
        reason=verdict.reason,
        error_signature=verdict.signature,
        subtask_id=subtask_id,
        contract_digest=contract_digest,
    )
