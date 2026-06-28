"""State synchronization for distributed workers.

Provides:
1. FileChangeEvent — broadcast file modifications via NATS (uc.file.changed)
2. ContextInjector — inject completed subtask summaries into dependent subtask prompts
3. WorkspaceStateMachine — track branch → commit → merge lifecycle

These ensure that distributed workers stay synchronized:
- File changes are broadcast so all workers see modifications
- Completed subtask context flows to dependent subtasks
- Workspace state is tracked for consistency

ponytail: NATS pub/sub for file events — simple, leverages existing
NATS infrastructure. Upgrade to CRDTs if conflict rate is high.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── File Change Events ────────────────────────────────────────────

class FileChangeEventType(Enum):
    """Type of file change event."""
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass
class FileChangeEvent:
    """Event emitted when a worker modifies a file.

    Published to NATS subject ``uc.file.changed`` so all workers
    and the orchestrator can track file modifications in real-time.
    """

    task_id: str = ""
    subtask_id: str = ""
    worker_id: str = ""
    file_path: str = ""
    change_type: FileChangeEventType = FileChangeEventType.MODIFIED
    diff_summary: str = ""  # first ~200 chars of diff
    timestamp: float = 0.0
    # For NATS dedup
    message_id: str = ""

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.time() * 1000
        if not self.message_id:
            bucket = int(self.timestamp) // 5000
            self.message_id = f"{self.task_id}:{self.subtask_id}:{self.file_path}:{bucket}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "worker_id": self.worker_id,
            "file_path": self.file_path,
            "change_type": self.change_type.value,
            "diff_summary": self.diff_summary[:200],
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileChangeEvent:
        return cls(
            task_id=data.get("task_id", ""),
            subtask_id=data.get("subtask_id", ""),
            worker_id=data.get("worker_id", ""),
            file_path=data.get("file_path", ""),
            change_type=FileChangeEventType(data.get("change_type", "modified")),
            diff_summary=data.get("diff_summary", ""),
            timestamp=data.get("timestamp", 0.0),
            message_id=data.get("message_id", ""),
        )


# ── Context Injection ─────────────────────────────────────────────

@dataclass
class SubtaskContext:
    """Context from completed subtasks to inject into dependent subtasks.

    When a subtask depends on other subtasks, the completed subtasks'
    summaries and key findings are injected into the dependent subtask's
    prompt so the worker has full context.
    """

    subtask_id: str = ""
    summary: str = ""
    modified_files: list[str] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)
    success: bool = True


class ContextInjector:
    """Inject completed subtask context into dependent subtask prompts.

    Collects results from completed subtasks and formats them into
    a context block that gets prepended to the dependent subtask's
    description/prompt.

    Usage:
        injector = ContextInjector()
        injector.add_result("st-1", summary="Fixed auth bug in login.py", files=["login.py"])
        context = injector.build_context(["st-1"])  # get context for subtask that depends on st-1
        prompt = f"{context}\n\n{subtask.description}"
    """

    def __init__(self, max_context_chars: int = 4000) -> None:
        self._results: dict[str, SubtaskContext] = {}
        self._max_context_chars = max_context_chars

    def add_result(
        self,
        subtask_id: str,
        summary: str = "",
        modified_files: list[str] | None = None,
        key_findings: list[str] | None = None,
        success: bool = True,
    ) -> None:
        """Record a completed subtask's result for context injection."""
        self._results[subtask_id] = SubtaskContext(
            subtask_id=subtask_id,
            summary=summary,
            modified_files=modified_files or [],
            key_findings=key_findings or [],
            success=success,
        )

    def build_context(self, depends_on: list[str]) -> str:
        """Build context block from completed dependencies.

        Args:
            depends_on: List of subtask IDs this subtask depends on.

        Returns:
            Formatted context string to prepend to the subtask prompt.
            Returns empty string if no matching results found.
        """
        if not depends_on:
            return ""

        # Collect matching results first
        matched: list[SubtaskContext] = []
        for dep_id in depends_on:
            ctx = self._results.get(dep_id)
            if ctx is not None:
                matched.append(ctx)

        if not matched:
            return ""

        parts: list[str] = []
        parts.append("## Context from completed subtasks\n")
        parts.append("The following subtasks have completed. Use their results as context:\n")

        for ctx in matched:
            parts.append(f"### Subtask {ctx.subtask_id[:8]} ({'✓' if ctx.success else '✗'})\n")
            if ctx.summary:
                # Truncate individual summaries
                summary = ctx.summary[:1000]
                parts.append(f"Summary: {summary}\n")
            if ctx.modified_files:
                parts.append(f"Modified files: {', '.join(ctx.modified_files[:10])}\n")
            if ctx.key_findings:
                for finding in ctx.key_findings[:5]:
                    parts.append(f"- {finding[:200]}\n")
            parts.append("")

        context = "\n".join(parts)
        # Truncate to max context size
        if len(context) > self._max_context_chars:
            context = context[: self._max_context_chars - 50] + "\n... (context truncated)"
        return context

    def get_file_state(self, depends_on: list[str]) -> dict[str, str]:
        """Get the expected file state after dependencies complete.

        Returns a dict of file_path → "modified" | "created" | "deleted"
        for all files touched by the dependencies.
        """
        file_state: dict[str, str] = {}
        for dep_id in depends_on:
            ctx = self._results.get(dep_id)
            if ctx is None:
                continue
            for fp in ctx.modified_files:
                if fp not in file_state:
                    file_state[fp] = "modified"
        return file_state

    def clear(self) -> None:
        """Clear all stored results."""
        self._results.clear()


# ── Workspace State Machine ───────────────────────────────────────

class WorkspaceState(Enum):
    """States in the workspace lifecycle."""
    CREATED = "created"
    BRANCHED = "branched"
    COMMITTED = "committed"
    MERGING = "merging"
    MERGED = "merged"
    CONFLICT = "conflict"
    ABORTED = "aborted"


@dataclass
class WorkspaceStateEntry:
    """Track the state of a workspace through its lifecycle."""
    workspace_id: str = ""
    subtask_id: str = ""
    branch_name: str = ""
    state: WorkspaceState = WorkspaceState.CREATED
    commit_sha: str = ""
    merge_conflict_files: list[str] = field(default_factory=list)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.time() * 1000


class WorkspaceStateMachine:
    """Track workspace state transitions for distributed coordination.

    Each workspace (per subtask) goes through:
    CREATED → BRANCHED → COMMITTED → MERGING → MERGED/CONFLICT

    This state machine is published via NATS so all workers can
    observe workspace progress and avoid operating on workspaces
    in incompatible states.
    """

    def __init__(self) -> None:
        self._states: dict[str, WorkspaceStateEntry] = {}

    def create(self, workspace_id: str, subtask_id: str, branch_name: str) -> WorkspaceStateEntry:
        """Record workspace creation."""
        entry = WorkspaceStateEntry(
            workspace_id=workspace_id,
            subtask_id=subtask_id,
            branch_name=branch_name,
            state=WorkspaceState.CREATED,
        )
        self._states[workspace_id] = entry
        return entry

    def transition(self, workspace_id: str, new_state: WorkspaceState, **kwargs: Any) -> WorkspaceStateEntry | None:
        """Transition a workspace to a new state.

        Valid transitions:
        CREATED → BRANCHED
        BRANCHED → COMMITTED
        COMMITTED → MERGING
        MERGING → MERGED | CONFLICT | ABORTED
        """
        entry = self._states.get(workspace_id)
        if entry is None:
            return None

        valid_transitions: dict[WorkspaceState, set[WorkspaceState]] = {
            WorkspaceState.CREATED: {WorkspaceState.BRANCHED},
            WorkspaceState.BRANCHED: {WorkspaceState.COMMITTED},
            WorkspaceState.COMMITTED: {WorkspaceState.MERGING},
            WorkspaceState.MERGING: {WorkspaceState.MERGED, WorkspaceState.CONFLICT, WorkspaceState.ABORTED},
        }

        allowed = valid_transitions.get(entry.state, set())
        if new_state not in allowed:
            logger.warning(
                "Invalid workspace state transition: %s → %s for %s",
                entry.state.value, new_state.value, workspace_id,
            )
            return entry

        entry.state = new_state
        entry.timestamp = time.time() * 1000
        if "commit_sha" in kwargs:
            entry.commit_sha = kwargs["commit_sha"]
        if "merge_conflict_files" in kwargs:
            entry.merge_conflict_files = kwargs["merge_conflict_files"]
        return entry

    def get_state(self, workspace_id: str) -> WorkspaceStateEntry | None:
        return self._states.get(workspace_id)

    def get_by_subtask(self, subtask_id: str) -> WorkspaceStateEntry | None:
        for entry in self._states.values():
            if entry.subtask_id == subtask_id:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for NATS broadcast."""
        return {
            ws_id: {
                "workspace_id": e.workspace_id,
                "subtask_id": e.subtask_id,
                "branch_name": e.branch_name,
                "state": e.state.value,
                "commit_sha": e.commit_sha,
                "merge_conflict_files": e.merge_conflict_files,
                "timestamp": e.timestamp,
            }
            for ws_id, e in self._states.items()
        }
