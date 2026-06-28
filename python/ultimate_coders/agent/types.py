"""Agent data types — Task, Subtask, WorkerInfo, and related enums."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    """Status of a top-level task."""
    CREATED = "created"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class SubtaskStatus(Enum):
    """Status of a subtask."""
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CONFLICTED = "conflicted"


class ChangeType(Enum):
    """Type of file change."""
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class DispatchMode(Enum):
    """How a subtask should be dispatched to workers."""
    LOCAL = "local"            # Execute locally (reserved, currently no-op)
    REMOTE = "remote"          # Must execute on remote worker, fail after 3 retries
    PREFER_REMOTE = "prefer_remote"  # Prefer remote, fallback to Pending (default)


@dataclass
class FileChange:
    """A file change produced by a worker."""
    file_path: str = ""
    change_type: ChangeType = ChangeType.MODIFIED
    diff: str = ""


class AdaptationStrategy(Enum):
    """How a Worker adapted after a failure."""
    NONE = "none"  # no adaptation needed
    SHRINK_SCOPE = "shrink_scope"  # timeout → reduce scope/timeout
    FALLBACK_TOOL = "fallback_tool"  # tool_not_found → use alternative tool
    PURE_LLM = "pure_llm"  # engine_error → skip tools, LLM-only
    WAIT_RETRY = "wait_retry"  # conflict_detected → wait then retry


@dataclass
class SubtaskResult:
    """Result from a completed subtask."""
    subtask_id: str = ""
    worker_id: str = ""
    modified_files: list[FileChange] = field(default_factory=list)
    summary: str = ""
    success: bool = True
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    adaptation_strategy: AdaptationStrategy = AdaptationStrategy.NONE
    # Failure context (populated on failure)
    stderr_tail: str = ""  # last ~10 lines of stderr
    recent_tool_calls: list[str] = field(default_factory=list)  # last ~5 tool names
    retry_count: int = 0  # how many retries this subtask used
    error: str = ""  # error message on failure


@dataclass
class Subtask:
    """A subtask assigned to a worker."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_id: str = ""
    description: str = ""
    status: SubtaskStatus = SubtaskStatus.PENDING
    assigned_worker: str | None = None
    depends_on: list[str] = field(default_factory=list)
    priority: int = 0
    file_constraints: list[str] = field(default_factory=list)
    expected_output: str = ""
    result: SubtaskResult | None = None
    retry_count: int = 0
    timeout_seconds: int = 0  # 0 = use default
    dispatch_mode: DispatchMode = DispatchMode.PREFER_REMOTE
    dispatch_retry_count: int = 0
    required_capabilities: list[str] = field(default_factory=list)
    # Per-subtask agent config overrides (keys: tools, allowed_tools,
    # disallowed_tools, mcp_configs, append_system_prompt, agent_name, agents_json)
    agent_config: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        """Whether this subtask has no unmet dependencies."""
        return self.status == SubtaskStatus.PENDING

    @property
    def is_complete(self) -> bool:
        """Whether this subtask has completed successfully."""
        return self.status == SubtaskStatus.COMPLETED

    @property
    def is_failed(self) -> bool:
        """Whether this subtask has failed."""
        return self.status == SubtaskStatus.FAILED


@dataclass
class Task:
    """A top-level task submitted by the user."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    project_id: str = ""
    status: TaskStatus = TaskStatus.CREATED
    subtasks: list[Subtask] = field(default_factory=list)
    result: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def update_timestamp(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(timezone.utc)

    # ponytail: to_dict for checkpoint serialization — called by orchestrator.checkpoint_task
    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization (checkpoint/recovery)."""
        return {
            "__version": 1,
            "id": self.id,
            "description": self.description,
            "project_id": self.project_id,
            "status": self.status.value,
            "subtasks": [
                {
                    "id": st.id,
                    "parent_id": st.parent_id,
                    "description": st.description,
                    "status": st.status.value,
                    "assigned_worker": st.assigned_worker,
                    "depends_on": st.depends_on,
                    "priority": st.priority,
                    "file_constraints": st.file_constraints,
                    "expected_output": st.expected_output,
                    "retry_count": st.retry_count,
                    "timeout_seconds": st.timeout_seconds,
                    "dispatch_mode": st.dispatch_mode.value,
                    "dispatch_retry_count": st.dispatch_retry_count,
                    "agent_config": st.agent_config,
                    "result": {
                        "subtask_id": st.result.subtask_id,
                        "worker_id": st.result.worker_id,
                        "modified_files": [
                            {
                                "path": fc.file_path,
                                "change_type": fc.change_type.value,
                                "diff_stats": fc.diff[:200] if fc.diff else "",
                            }
                            for fc in st.result.modified_files
                        ],
                        "summary": st.result.summary,
                        "success": st.result.success,
                        "completed_at": st.result.completed_at.isoformat(),
                        "adaptation_strategy": st.result.adaptation_strategy.value,
                        "stderr_tail": st.result.stderr_tail,
                        "recent_tool_calls": st.result.recent_tool_calls,
                        "retry_count": st.result.retry_count,
                        "error": st.result.error,
                    } if st.result else None,
                }
                for st in self.subtasks
            ],
            "result": self.result,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Reconstruct a Task from a checkpoint dict (inverse of to_dict).

        ponytail: handles version 1 format; ignores unknown keys for forward compat.
        """
        task = cls(
            id=data.get("id", ""),
            description=data.get("description", ""),
            project_id=data.get("project_id", ""),
            status=TaskStatus(data["status"]) if "status" in data else TaskStatus.CREATED,
            result=data.get("result"),
        )
        if "created_at" in data:
            task.created_at = datetime.fromisoformat(data["created_at"])
        if "updated_at" in data:
            task.updated_at = datetime.fromisoformat(data["updated_at"])
        task.subtasks = []
        for sd in data.get("subtasks", []):
            st = Subtask(
                id=sd.get("id", ""),
                parent_id=sd.get("parent_id", ""),
                description=sd.get("description", ""),
                status=SubtaskStatus(sd["status"]) if "status" in sd else SubtaskStatus.PENDING,
                assigned_worker=sd.get("assigned_worker"),
                depends_on=sd.get("depends_on", []),
                priority=sd.get("priority", 0),
                file_constraints=sd.get("file_constraints", []),
                expected_output=sd.get("expected_output", ""),
                retry_count=sd.get("retry_count", 0),
                timeout_seconds=sd.get("timeout_seconds", 0),
                dispatch_mode=(
                    DispatchMode(sd["dispatch_mode"])
                    if "dispatch_mode" in sd
                    else DispatchMode.PREFER_REMOTE
                ),
                dispatch_retry_count=sd.get("dispatch_retry_count", 0),
                required_capabilities=sd.get("required_capabilities", []),
                agent_config=sd.get("agent_config", {}),
            )
            rd = sd.get("result")
            if rd is not None:
                st.result = SubtaskResult(
                    subtask_id=rd.get("subtask_id", ""),
                    worker_id=rd.get("worker_id", ""),
                    summary=rd.get("summary", ""),
                    success=rd.get("success", True),
                    adaptation_strategy=AdaptationStrategy(rd.get("adaptation_strategy", "none")),
                    stderr_tail=rd.get("stderr_tail", ""),
                    recent_tool_calls=rd.get("recent_tool_calls", []),
                    retry_count=rd.get("retry_count", 0),
                    error=rd.get("error", ""),
                )
                if "modified_files" in rd:
                    for fc in rd["modified_files"]:
                        st.result.modified_files.append(FileChange(
                            file_path=fc.get("path", ""),
                            change_type=ChangeType(fc.get("change_type", "modified")),
                            diff=fc.get("diff_stats", ""),
                        ))
                if "completed_at" in rd:
                    st.result.completed_at = datetime.fromisoformat(rd["completed_at"])
            task.subtasks.append(st)
        return task

    @property
    def is_complete(self) -> bool:
        """Whether all subtasks have completed successfully."""
        return (
            len(self.subtasks) > 0
            and all(st.is_complete for st in self.subtasks)
        )

    @property
    def has_failed(self) -> bool:
        """Whether any subtask has failed and cannot be retried."""
        return any(st.is_failed for st in self.subtasks)

    @property
    def ready_subtasks(self) -> list[Subtask]:
        """Subtasks that are pending and have all dependencies met."""
        completed_ids = {st.id for st in self.subtasks if st.is_complete}
        return [
            st for st in self.subtasks
            if st.is_ready and all(dep in completed_ids for dep in st.depends_on)
        ]


@dataclass
class WorkerInfo:
    """Information about a registered worker."""
    id: str = ""
    capabilities: list[str] = field(default_factory=list)
    current_load: int = 0
    max_capacity: int = 3
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_available(self) -> bool:
        """Whether the worker can accept more subtasks."""
        return self.current_load < self.max_capacity


@dataclass
class OrchestratorConfig:
    """Configuration for the Orchestrator."""
    max_subtasks: int = 10
    max_retries: int = 3
    heartbeat_timeout_seconds: int = 60
    subtask_timeout_seconds: int = 600  # 10 min default per subtask
    # LLM planning context budget (tokens). Tool-calling loop stops
    # gathering context when cumulative tokens approach this limit.
    planning_context_budget: int = 50000
    # Max tool-calling rounds for plan_task / ask
    planning_max_tool_rounds: int = 5
    # Max chars per tool result (truncated if exceeded)
    tool_result_max_chars: int = 2000


# ── Agent Loop Types ──────────────────────────────────────────────


class AgentEventType(Enum):
    """Event types emitted by the Orchestrator's agent loop."""
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    AGENT_ERROR = "agent_error"


@dataclass
class AgentEvent:
    """A single event from the agent loop."""
    type: AgentEventType
    turn: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunConfig:
    """Configuration for a single agent loop run."""
    max_turns: int = 5
    token_budget: int = 50000
    abort_event: asyncio.Event | None = None
    steering_queue: asyncio.Queue | None = None


@dataclass
class ExecutionSpec:
    """Structured output from plan_task() — an execution spec, not a design doc.

    Every choice is pre-made so an implementer can execute top-to-bottom
    with ZERO design decisions.
    """
    context: str = ""
    approach: list[str] = field(default_factory=list)
    critical_files: list[str] = field(default_factory=list)
    verification: str = ""
    assumptions: str = ""
    raw_text: str = ""  # fallback for sandbox decomposition
