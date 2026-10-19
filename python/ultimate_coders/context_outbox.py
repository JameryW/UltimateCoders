"""Context-compilation manifests: one record per compile, for post-hoc audit.

The terminal ``outbox.OutcomeOutbox`` answers "what happened to this
subtask" with one record per subtask and a delivery state machine. This
outbox answers a different question: "what did the prompt look like when
this attempt started". Every ``compile_context`` call leaves one snapshot
-- budget, tier counts, drop reasons, risk profile -- so an auditor can
reconstruct why a run saw less context than expected after the worker is
long gone.

Two deliberate differences from the terminal outbox:

* the key includes the attempt. An outcome is one current fact per
  subtask (a retry supersedes it); a manifest is a historical snapshot,
  so if a retry ever recompiles, both snapshots must survive.
* there is no delivery state. Writing the record *is* the delivery --
  nothing replays these, the database is the audit trail.

Snapshots carry no prompt content: ``ContextManifest.summary()`` is
log-safe by contract, and only that summary is stored.
"""

from __future__ import annotations

import time

from .runtime_state import RuntimeState

#: Namespace inside ``RuntimeState``; separate from ``result_outbox`` so a
#: query for terminal outcomes never picks up audit snapshots.
NAMESPACE = "context_manifest"


def manifest_key(task_id: str, subtask_id: str, attempt: int) -> str:
    """Identity of one compile: the subtask *and* the attempt.

    Unlike ``outbox.outcome_key`` the attempt is part of the key on
    purpose -- see the module docstring.
    """
    return f"{task_id}:{subtask_id}:{attempt}"


class ContextManifestOutbox:
    def __init__(self, state: RuntimeState):
        self.state = state

    def record(self, key: str, *, summary: dict) -> dict:
        """Store one compile's summary under ``key``.

        A repeat write with identical content is a no-op (a resend of the
        same compile is not a new fact); a repeat with different content
        replaces it, because the latest compile is what the attempt
        actually used. Either way the stored record is the summary plus
        the first time it was seen.
        """

        def change(old: dict) -> dict:
            if old and old.get("summary") == summary:
                return old
            return {"summary": summary, "recorded_at": time.time()}

        return self.state.mutate(NAMESPACE, key, change)

    def history(self, task_id: str, subtask_id: str, *, limit: int = 100) -> list[dict]:
        """Every recorded compile for one subtask, oldest key first.

        Returns the raw records (``key``, ``summary``, ``recorded_at``);
        decoding is the auditor's job.
        """
        return self.state.query(
            NAMESPACE, prefix=f"{task_id}:{subtask_id}:", limit=limit
        )

    def task_history(self, task_id: str, *, limit: int = 100) -> list[dict]:
        """Every recorded compile for one task, across all its subtasks.

        Ordering is key order, which is subtask-lexicographic then
        attempt-lexicographic (``s10`` sorts before ``s2``); callers that
        need attempt-numeric order re-sort. The trailing ``:`` in the
        prefix keeps ``task-1`` from swallowing ``task-10``.
        """
        return self.state.query(NAMESPACE, prefix=f"{task_id}:", limit=limit)
