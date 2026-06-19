"""Agent data types — Task, Subtask, WorkerInfo, and related enums."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


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


@dataclass
class FileChange:
    """A file change produced by a worker."""
    file_path: str = ""
    change_type: ChangeType = ChangeType.MODIFIED
    diff: str = ""


@dataclass
class SubtaskResult:
    """Result from a completed subtask."""
    subtask_id: str = ""
    worker_id: str = ""
    modified_files: list[FileChange] = field(default_factory=list)
    summary: str = ""
    success: bool = True
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
