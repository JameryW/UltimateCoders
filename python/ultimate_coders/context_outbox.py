"""Context-compilation manifests: one snapshot per compile identity, for audit.

The terminal ``outbox.OutcomeOutbox`` answers "what happened to this
subtask" with one record per subtask and a delivery state machine. This
outbox answers a different question: "what did the prompt look like when
this attempt started". Every ``compile_context`` call writes a snapshot
under its attempt's key -- budget, tier counts, drop reasons, risk profile
-- so an auditor can reconstruct why a run saw less context than expected
after the worker is long gone. The key is ``task:subtask:attempt``: a
*recompile of the same attempt* (a redelivered subtask that executes
again) replaces that attempt's earlier snapshot, including its
``recorded_at``, so the record names the summary the attempt last used and
when that summary was recorded. The replaced snapshot is not recoverable;
the box keeps per-attempt history, not per-call history.

Two deliberate differences from the terminal outbox:

* the key includes the attempt. An outcome is one current fact per
  subtask (a retry supersedes it); a manifest is a per-attempt snapshot,
  so a re-dispatched attempt keeps its own record instead of overwriting
  the earlier attempt's.
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

#: Rows per ``state.query`` call while ``recent()`` walks the key cursor.
#: ``query`` caps ``limit`` at 1000, so this is the widest legal page; the
#: pagination test narrows it to prove the walk crosses pages.
SCAN_PAGE = 1000


class CursorNotFoundError(ValueError):
    """The pagination cursor names no record in the scanned window.

    A ``ValueError`` subclass so callers that only guard "bad pagination"
    still catch it; the dashboard endpoint maps it to its own 400 message.
    """


def manifest_key(task_id: str, subtask_id: str, attempt: int) -> str:
    """Identity of one compile: the subtask *and* the attempt.

    Unlike ``outbox.outcome_key`` the attempt is part of the key on
    purpose -- see the module docstring. ``attempt`` is the *dispatch*
    attempt (the worker passes ``dispatch_retry_count`` -- the same
    identity its checkpoint and terminal-delivery keys use), not an
    in-process retry counter: the compiler runs once per dispatch, so a
    re-dispatch must land beside the earlier snapshot instead of
    overwriting it.
    """
    return f"{task_id}:{subtask_id}:{attempt}"


class ContextManifestOutbox:
    def __init__(self, state: RuntimeState):
        self.state = state

    def record(self, key: str, *, summary: dict) -> dict:
        """Store one compile's summary under ``key``.

        A repeat write with identical content is a no-op (a resend of the
        same compile is not a new fact) and keeps the original
        ``recorded_at``. A repeat with different content replaces the
        record *and re-stamps* ``recorded_at``: the latest compile is what
        the attempt actually used, so the surviving summary carries the
        time it was compiled, not its predecessor's. (The earlier wording
        claimed "the first time it was seen" for both paths; the code
        never did that, and keeping the old clock would file a recompile
        under a time older than the content it stored.)
        """

        def change(old: dict) -> dict:
            if old and old.get("summary") == summary:
                return old
            return {"summary": summary, "recorded_at": time.time()}

        return self.state.mutate(NAMESPACE, key, change)

    def history(self, task_id: str, subtask_id: str, *, limit: int = 100) -> list[dict]:
        """Up to ``limit`` recorded compiles for one subtask, oldest key first.

        Truncation is in key order, so when the trail is longer than
        ``limit`` the window holds the *oldest* records and drops the
        newest attempts -- "every recorded compile" was the old wording
        and is false past the limit. Callers that need the newest
        compiles want ``recent``; callers that need to know how many
        exist want ``total``.

        Returns the raw records (``key``, ``summary``, ``recorded_at``);
        decoding is the auditor's job.
        """
        return self.state.query(
            NAMESPACE, prefix=f"{task_id}:{subtask_id}:", limit=limit
        )

    def task_history(self, task_id: str, *, limit: int = 100) -> list[dict]:
        """Up to ``limit`` recorded compiles for one task, across its subtasks.

        Same oldest-key window as ``history``: past ``limit`` the newest
        records are the ones dropped. Ordering is key order, which is
        subtask-lexicographic then attempt-lexicographic (``s10`` sorts
        before ``s2``); callers that need attempt-numeric order re-sort.
        The trailing ``:`` in the prefix keeps ``task-1`` from swallowing
        ``task-10``.
        """
        return self.state.query(NAMESPACE, prefix=f"{task_id}:", limit=limit)

    def total(self, task_id: str, *, subtask_id: str | None = None) -> int:
        """How many compiles exist in scope -- the scan count, not a window.

        ``history``/``task_history`` return at most ``limit`` rows and
        ``count == len(manifests)`` on the wire is a window size, so a
        caller displaying either as "N recorded" overstates nothing when
        the trail is short and understates it (silently) when it is not.
        This counts the whole ``task:``/``task:subtask:`` prefix on the
        key cursor -- the same bounded walk ``recent`` documents, one
        pass over the task's compiles.
        """
        prefix = f"{task_id}:{subtask_id}:" if subtask_id else f"{task_id}:"
        counted = 0
        after = ""
        while True:
            page = self.state.query(NAMESPACE, prefix=prefix, after=after, limit=SCAN_PAGE)
            counted += len(page)
            if len(page) < SCAN_PAGE:
                break
            after = page[-1]["key"]
        return counted

    def recent(
        self,
        task_id: str,
        *,
        subtask_id: str | None = None,
        limit: int = 100,
        before_key: str | None = None,
    ) -> list[dict]:
        """The newest compiles by ``recorded_at``, newest first.

        This is a *time* window, not a key window. ``RuntimeState.query``
        truncates in key order (``ORDER BY record_key LIMIT n``), so a
        "fetch ``limit`` rows, then sort by time" implementation answers
        "the newest of the first ``limit`` keys" and drops exactly the
        record this method exists to surface: the one whose key sorts
        last while its ``recorded_at`` is newest. So the scan walks the
        whole ``task:``/``task:subtask:`` prefix on the key cursor and
        truncates only *after* the time sort -- ``limit`` bounds the
        response, never the scan (one pass over the task's compiles,
        which an audit read can pay).

        Ordering is the total key ``(-recorded_at, key)``: newest first,
        ties by key ascending. ``subtask_id`` of ``None``/empty scans the
        whole task; the trailing-``:`` prefix boundary is the same one
        ``task_history`` documents (``task-1`` never swallows
        ``task-10``).

        ``before_key`` pages through that total order: it names the last
        row of the previous page and the response starts *after* it. The
        anchor is a record key, not an offset, so records written between
        pages do not shift the walk (they are newer than the anchor and
        are simply not re-served; a record re-stamped past the anchor is
        likewise not seen again until a fresh first page). A key outside
        this scan's scope raises ``CursorNotFoundError`` instead of silently
        restarting (which would serve the first page twice).
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Query limit must be between 1 and 1000")
        if before_key is not None and (
            not isinstance(before_key, str) or not before_key
        ):
            raise ValueError("Cursor must be a non-empty key string")
        prefix = f"{task_id}:{subtask_id}:" if subtask_id else f"{task_id}:"
        window: list[dict] = []
        after = ""
        while True:
            page = self.state.query(NAMESPACE, prefix=prefix, after=after, limit=SCAN_PAGE)
            window.extend(page)
            if len(page) < SCAN_PAGE:
                break
            after = page[-1]["key"]
        window.sort(key=lambda r: (-r["recorded_at"], r["key"]))
        if before_key is not None:
            for position, record in enumerate(window):
                if record["key"] == before_key:
                    window = window[position + 1:]
                    break
            else:
                raise CursorNotFoundError(f"Unknown cursor {before_key!r}")
        return window[:limit]
