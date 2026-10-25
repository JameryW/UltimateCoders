"""Task contract identity: a goal that changed mid-run must not be continued silently.

A subtask is derived from a *contract* -- the original request text carrying
the output and safety constraints. When that text changes between two
dispatches of the same subtask, the work already done was done against a
different goal. Continuing silently would report success for a result nobody
asked for any more.

This module records, per task, the digest of the contract each dispatch was
issued under. The first dispatch establishes version 1; an identical digest
is a routine retry; a different digest bumps the version and records the
previous one. The worker refuses to execute on a mismatch and reports
``contract_changed`` instead -- re-planning under the new contract is the
orchestrator's job, not something a worker can improvise.

The digest normalizes only cosmetics (CRLF, trailing whitespace, blank-line
edges). Wording changes are *not* normalized away: "must not touch main" and
"may touch main" differ by one word and by the entire risk posture.

A second namespace — structured ``completion_conditions`` — lives beside digest
and is re-exported here (D3 / ticket 05). Digest responsibility remains
goal-change detection only; completion evaluation is non-LLM and does not
mutate the registry. See :mod:`ultimate_coders.agent.completion_conditions`.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from ..runtime_state import RuntimeState
from .completion_conditions import (
    COMPLETION_CONDITIONS_ENFORCE_ENV,
    CompletionCondition,
    CompletionVerdict,
    ConditionResult,
    completion_conditions_enforce_enabled,
    completion_gate,
    evaluate_completion_conditions,
)

__all__ = [
    "NAMESPACE",
    "contract_digest",
    "ContractVerdict",
    "ContractRegistry",
    # completion_conditions namespace (D3)
    "COMPLETION_CONDITIONS_ENFORCE_ENV",
    "CompletionCondition",
    "ConditionResult",
    "CompletionVerdict",
    "completion_conditions_enforce_enabled",
    "evaluate_completion_conditions",
    "completion_gate",
]

#: Namespace holding one record per task (key = task id).
NAMESPACE = "task_contracts"


def contract_digest(text: str) -> str:
    """Digest of a contract text, normalized for cosmetics only."""
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    body = "\n".join(lines)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractVerdict:
    """Outcome of observing one dispatch's contract."""

    status: str  # "first_seen" | "unchanged" | "changed"
    version: int
    previous_version: int = 0
    previous_digest: str = ""

    @property
    def changed(self) -> bool:
        return self.status == "changed"


class ContractRegistry:
    """Durable per-task contract versions on top of RuntimeState."""

    def __init__(self, state: RuntimeState, *, namespace: str = NAMESPACE) -> None:
        self.state = state
        self.namespace = namespace

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.state.get(self.namespace, task_id)

    def observe(self, task_id: str, text: str, *, excerpt_limit: int = 200) -> ContractVerdict:
        """Record the digest this dispatch was issued under.

        First observation establishes version 1; the same digest is
        ``unchanged``; a new digest bumps the version and keeps the previous
        digest/version for audit. The verdict is decided inside the
        transaction, so two concurrent observers cannot both see
        ``first_seen``.
        """
        digest = contract_digest(text)
        now = time.time()
        excerpt = str(text or "").strip()[:excerpt_limit]

        def change(old: dict) -> dict:
            if not old:
                return {
                    "task_id": task_id,
                    "version": 1,
                    "digest": digest,
                    "excerpt": excerpt,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_verdict": "first_seen",
                }
            if old.get("digest") == digest:
                return {**old, "last_seen_at": now, "last_verdict": "unchanged"}
            return {
                **old,
                "version": int(old.get("version", 1)) + 1,
                "digest": digest,
                "excerpt": excerpt,
                "previous_digest": old.get("digest", ""),
                "previous_version": int(old.get("version", 1)),
                "last_seen_at": now,
                "changed_at": now,
                "last_verdict": "changed",
            }

        record = self.state.mutate(self.namespace, task_id, change)
        return ContractVerdict(
            str(record.get("last_verdict", "unchanged")),
            int(record.get("version", 1)),
            int(record.get("previous_version", 0)),
            str(record.get("previous_digest", "")),
        )

    def current_digest(self, task_id: str) -> str:
        record = self.get(task_id) or {}
        return str(record.get("digest", ""))
