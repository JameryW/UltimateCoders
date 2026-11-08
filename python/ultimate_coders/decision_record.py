"""Decision Records: one audit join per dispatch attempt.

Route choice, context-compile identity, and terminal verdict already exist as
separate trails. Auditors need one attempt-scoped object that joins them
without inventing a second scheduler or storing prompt content. The key is
``task:subtask:attempt`` — the same dispatch identity Context Manifest and
checkpoint use — so a re-dispatch lands beside the earlier record instead of
overwriting it.

Start facts create the record; terminal facts merge into the same key.
Identical start content is a no-op that keeps ``recorded_at``; a different
start replaces start fields, re-stamps ``recorded_at``, and preserves any
existing terminal. Terminal always re-stamps ``terminal_at``. The terminal block
may also carry a compact ``workspace`` summary (changed paths + tool names,
never content) so the attempt's local effects are auditable without per-tool
interception (D17). Persistence is best-effort at the worker: losing a record
must never fail the subtask.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .runtime_state import RuntimeState

#: Namespace inside ``RuntimeState``; separate from ``context_manifest`` and
#: ``result_outbox`` so a query for one trail never picks up another.
NAMESPACE = "decision_record"

#: Rows per ``state.query`` call while ``recent()`` / ``total()`` walk the
#: key cursor. ``query`` caps ``limit`` at 1000, so this is the widest legal
#: page; tests narrow it to prove the walk crosses pages.
SCAN_PAGE = 1000

_START_KEYS = ("worker_id", "route", "context_digest", "context_key", "governance")

#: Caps for the attempt-scoped workspace evidence. The record is a summary, not
#: a diff: past these limits the block says ``truncated`` instead of growing.
WORKSPACE_FILE_LIMIT = 50
WORKSPACE_TOOL_LIMIT = 20


def workspace_summary(modified_files, tool_calls) -> dict[str, Any] | None:
    """Compact, log-safe summary of an attempt's local effects.

    ``modified_files`` may hold ``FileChange``-like objects (``file_path`` /
    ``change_type``) or mappings; paths are sorted and de-duplicated, tool names
    keep first-seen order and repeat once only. Content (``FileChange.diff``)
    is **never** copied -- the record must stay log-safe.

    Returns ``None`` when there is nothing to say (no changed file and no tool
    name): an empty block would claim "ran and changed nothing" even on paths
    where no agent ran at all.
    """
    files: dict[str, str] = {}
    for item in modified_files or ():
        if isinstance(item, dict):
            path = str(item.get("file_path") or item.get("path") or "").strip()
            change = item.get("change_type") or item.get("status") or "modified"
        else:
            path = str(getattr(item, "file_path", "") or "").strip()
            change = getattr(item, "change_type", "modified")
        if not path:
            continue
        # ChangeType is an Enum; ``str()`` would store "ChangeType.MODIFIED".
        files[path] = getattr(change, "value", None) or str(change)
    ordered = [{"path": path, "change_type": files[path]} for path in sorted(files)]

    tools: list[str] = []
    for name in tool_calls or ():
        text = str(name or "").strip()
        if text and text not in tools:
            tools.append(text)

    if not ordered and not tools:
        return None
    return {
        "files": ordered[:WORKSPACE_FILE_LIMIT],
        "file_count": len(ordered),
        "tool_calls": tools[:WORKSPACE_TOOL_LIMIT],
        "truncated": len(ordered) > WORKSPACE_FILE_LIMIT or len(tools) > WORKSPACE_TOOL_LIMIT,
    }


class CursorNotFoundError(ValueError):
    """The pagination cursor names no record in the scanned window.

    A ``ValueError`` subclass so callers that only guard "bad pagination"
    still catch it; the dashboard endpoint maps it to its own 400 message.
    """


def decision_key(task_id: str, subtask_id: str, attempt: int) -> str:
    """Identity of one Decision Record: the subtask *and* the dispatch attempt.

    ``attempt`` is ``dispatch_retry_count`` — the same identity context
    manifests and checkpoints use — not an in-process retry counter.
    """
    return f"{task_id}:{subtask_id}:{attempt}"


def context_digest(summary: dict) -> str:
    """Stable SHA-256 of a context-manifest summary (canonical JSON).

    Never hashes prompt content: callers pass ``ContextManifest.summary()``,
    which is log-safe by contract. ``sort_keys`` + compact separators keep
    the digest independent of dict insertion order.
    """
    return hashlib.sha256(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _start_fields(
    *,
    worker_id: str,
    route: dict | None,
    context_digest: str | None,
    context_key: str | None,
    governance: dict | None,
) -> dict[str, Any]:
    """Build the stored start payload, omitting absent optional fields."""
    start: dict[str, Any] = {"worker_id": worker_id}
    if route is not None:
        start["route"] = route
    if context_digest is not None:
        start["context_digest"] = context_digest
    if context_key is not None:
        start["context_key"] = context_key
    if governance is not None:
        start["governance"] = governance
    return start


def _start_norm(record: dict) -> dict[str, Any]:
    """Normalize start fields for equality (missing == None)."""
    return {key: record.get(key) for key in _START_KEYS}


class DecisionRecordOutbox:
    def __init__(self, state: RuntimeState):
        self.state = state

    def record_start(
        self,
        key: str,
        *,
        worker_id: str,
        route: dict | None = None,
        context_digest: str | None = None,
        context_key: str | None = None,
        governance: dict | None = None,
    ) -> dict:
        """Create or replace start facts under ``key``.

        Identical start content is a no-op and keeps the original
        ``recorded_at``. Different start content replaces the start fields,
        re-stamps ``recorded_at``, and preserves any existing ``terminal`` /
        ``terminal_at`` so a late start rewrite cannot erase a verdict.
        """
        start = _start_fields(
            worker_id=worker_id,
            route=route,
            context_digest=context_digest,
            context_key=context_key,
            governance=governance,
        )
        new_norm = {
            "worker_id": worker_id,
            "route": route,
            "context_digest": context_digest,
            "context_key": context_key,
            "governance": governance,
        }

        def change(old: dict) -> dict:
            if old and _start_norm(old) == new_norm:
                return old
            updated = {**start, "recorded_at": time.time()}
            if old.get("terminal") is not None:
                updated["terminal"] = old["terminal"]
            if "terminal_at" in old:
                updated["terminal_at"] = old["terminal_at"]
            return updated

        return self.state.mutate(NAMESPACE, key, change)

    def record_terminal(
        self,
        key: str,
        *,
        success: bool,
        summary: str | None = None,
        error_class: str | None = None,
        oracle: dict | None = None,
        review: dict | None = None,
        workspace: dict | None = None,
    ) -> dict:
        """Merge terminal facts into ``key`` and always re-stamp ``terminal_at``.

        Compact only: truncated summary / error class / optional oracle, review
        or workspace evidence — never tool stdout, diffs or prompt content. A
        terminal write with no prior start still creates a record so a late-join
        auditor can see the verdict.
        """
        terminal: dict[str, Any] = {"success": success}
        if summary is not None:
            terminal["summary"] = summary
        if error_class is not None:
            terminal["error_class"] = error_class
        if oracle is not None:
            terminal["oracle"] = oracle
        if review is not None:
            terminal["review"] = review
        if workspace is not None:
            terminal["workspace"] = workspace

        def change(old: dict) -> dict:
            updated = dict(old) if old else {}
            updated["terminal"] = terminal
            updated["terminal_at"] = time.time()
            return updated

        return self.state.mutate(NAMESPACE, key, change)

    def history(self, task_id: str, subtask_id: str, *, limit: int = 100) -> list[dict]:
        """Up to ``limit`` records for one subtask, oldest key first."""
        return self.state.query(
            NAMESPACE, prefix=f"{task_id}:{subtask_id}:", limit=limit
        )

    def task_history(self, task_id: str, *, limit: int = 100) -> list[dict]:
        """Up to ``limit`` records for one task, across its subtasks.

        Ordering is key order (subtask-lexicographic then attempt-lexicographic);
        callers that need attempt-numeric order re-sort. The trailing ``:``
        keeps ``task-1`` from swallowing ``task-10``.
        """
        return self.state.query(NAMESPACE, prefix=f"{task_id}:", limit=limit)

    def total(self, task_id: str, *, subtask_id: str | None = None) -> int:
        """How many records exist in scope — the scan count, not a window."""
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
        """The newest records by ``recorded_at``, newest first.

        Same time-window contract as ``ContextManifestOutbox.recent``: the
        scan walks the whole prefix before truncating, so ``limit`` bounds
        the response, never the scan.
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
        window.sort(key=lambda r: (-r.get("recorded_at", 0), r["key"]))
        if before_key is not None:
            for position, record in enumerate(window):
                if record["key"] == before_key:
                    window = window[position + 1 :]
                    break
            else:
                raise CursorNotFoundError(f"Unknown cursor {before_key!r}")
        return window[:limit]
