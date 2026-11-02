"""Consumer-side submit dedup — the second half of PR01's at-least-once contract.

The gateway guarantees that one ``submission_id`` names at most one logical task
(``uc-types/src/governance/submission.rs::derive_task_id``) and publishes it
through a transactional outbox. That gives **at-least-once delivery**, by design:
a crash between publish and acknowledgement, or a dispatcher restarted mid-flight,
re-delivers the same submission.

At-least-once is only safe if the consumer is idempotent, and that is what this
module provides: a durable record per ``(submission_id, task_id)`` that says
whether the submission has already been applied to the orchestrator.

Record states (``uc_runtime_records`` namespace ``task_submission``)::

    {"attempts": n, "applied": false}                 observed, not yet applied
    {"attempts": n, "applied": true, "task_id": "…"}  applied; duplicates are dropped

A crash between *observe* and *mark_applied* leaves ``applied: false``, so the
redelivery is processed again. That is the correct direction: re-applying the same
logical task id is harmless (the orchestrator keys tasks by that id), while
dropping a submission that never landed would lose work.

Honest limitation: the dedup record lives in the same runtime-state database as
the rest of the worker's coordination records. If that database is wiped while the
gateway keeps its submission record, a re-delivery is applied again. The gateway's
``TaskStore`` insert is the backstop in that scenario (duplicate task id → the
insert fails rather than creating a second task).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Runtime-state namespace holding one record per (submission, task) pair.
NAMESPACE = "task_submission"

#: Bounded number of attempts kept in the record, to keep the document small.
MAX_RECORDED_ATTEMPTS = 100


class SubmissionClaim:
    """Result of observing a submit payload."""

    __slots__ = ("duplicate", "attempts", "applied_task_id")

    def __init__(self, duplicate: bool, attempts: int, applied_task_id: str | None) -> None:
        self.duplicate = duplicate
        self.attempts = attempts
        self.applied_task_id = applied_task_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SubmissionClaim(duplicate={self.duplicate!r}, "
            f"attempts={self.attempts!r}, applied_task_id={self.applied_task_id!r})"
        )


def submission_identity(payload: dict[str, Any]) -> tuple:
    """Extract ``(submission_id, task_id)`` from a ``uc.task.submit`` payload.

    Absent values become empty strings. A payload with neither is *not* deduped:
    an older gateway (pre-PR01) sends no submission id, and inventing one would
    collapse unrelated submissions.
    """
    submission_id = str(payload.get("submission_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    return submission_id, task_id


def record_key(submission_id: str, task_id: str) -> str | None:
    """Stable key for the dedup record, or ``None`` when there is nothing to key on."""
    if submission_id and task_id:
        return f"{submission_id}|{task_id}"
    if task_id:
        # Legacy submission without an idempotency key: dedupe on the derived task
        # id only. Still catches the common "same message delivered twice" case.
        return f"task:{task_id}"
    return None


class SubmissionDedup:
    """Durable "has this submission already been applied?" ledger."""

    def __init__(self, state: Any) -> None:
        self._state = state

    def observe(self, submission_id: str, task_id: str) -> SubmissionClaim:
        """Record that a submission is being processed; report duplicates.

        Sync on purpose: the worker calls it through ``asyncio.to_thread`` so the
        SQLite/PostgreSQL write never blocks the event loop (same convention as the
        rest of ``runtime_state`` usage).
        """
        key = record_key(submission_id, task_id)
        if key is None:
            return SubmissionClaim(False, 0, None)

        def change(current: dict[str, Any]) -> dict[str, Any]:
            attempts = int(current.get("attempts") or 0) + 1
            current["attempts"] = min(attempts, MAX_RECORDED_ATTEMPTS)
            current["submission_id"] = submission_id
            current["task_id"] = task_id
            current["last_observed_at"] = time.time()
            current.setdefault("applied", False)
            return current

        record = self._state.mutate(NAMESPACE, key, change)
        applied = bool(record.get("applied"))
        return SubmissionClaim(
            duplicate=applied,
            attempts=int(record.get("attempts") or 0),
            applied_task_id=record.get("orchestrator_task_id") if applied else None,
        )

    def mark_applied(
        self, submission_id: str, task_id: str, orchestrator_task_id: str | None = None
    ) -> None:
        """Record that the orchestrator accepted the submission."""
        key = record_key(submission_id, task_id)
        if key is None:
            return

        def change(current: dict[str, Any]) -> dict[str, Any]:
            current["applied"] = True
            current["applied_at"] = time.time()
            current["submission_id"] = submission_id
            current["task_id"] = task_id
            if orchestrator_task_id:
                current["orchestrator_task_id"] = orchestrator_task_id
            return current

        self._state.mutate(NAMESPACE, key, change)

    def is_applied(self, submission_id: str, task_id: str) -> bool:
        """Read-only probe (tests, dashboards, and the duplicate reply path)."""
        key = record_key(submission_id, task_id)
        if key is None:
            return False
        record = self._state.get(NAMESPACE, key)
        return bool(record and record.get("applied"))


__all__ = [
    "NAMESPACE",
    "MAX_RECORDED_ATTEMPTS",
    "SubmissionClaim",
    "SubmissionDedup",
    "record_key",
    "submission_identity",
]
