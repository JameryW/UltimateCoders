"""Minimal Orchestrator — lightweight task + worker state management.

Replaces the full Python Orchestrator (removed in PR #161) with just enough
functionality for nats_worker.py to operate.

Provides:
- Task CRUD (submit, assign subtask, handle result, get status)
- Worker registration + heartbeat tracking
- Conflict detection (stubs)
- Event emission (via TaskEventEmitter or NATS publisher)
- Subtask selection (simple DAG-ordered ready check)
- Pause/resume/cancel

NOT provided (was in full Orchestrator, now handled by OMP extension):
- LLM-based task decomposition
- Scheduler (cron jobs)
- Dashboard snapshot (nats_worker builds its own)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ultimate_coders.agent.conflict import ConflictDetector
from ultimate_coders.agent.event_emitter import TaskEventEmitter
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)
from ultimate_coders.agent.worker import WorkerInfo

logger = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────


@dataclass
class OrchestratorConfig:
    """Minimal config — matches what nats_worker access."""
    max_retries: int = 3
    heartbeat_timeout_seconds: int = 90
    # ponytail: remaining fields are stubs that workers check but don't functionally use
    scheduler: Any = None
    night_window: Any = None


# ── Worker tracking ────────────────────────────────────────────


@dataclass
class WorkerEntry:
    """Tracks a registered worker's state."""
    id: str
    capabilities: list[str] = field(default_factory=list)
    current_load: int = 0
    max_capacity: int = 3
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_available(self) -> bool:
        return self.current_load < self.max_capacity


# ── MinimalOrchestrator ────────────────────────────────────────


class Orchestrator:
    """Lightweight orchestrator for nats_worker.

    Provides just enough state management to run tasks through Workers
    without the full LLM decomposition / scheduler / dashboard stack.
    """

    def __init__(
        self,
        engine: Any = None,
        nats_publisher: Any = None,
        llm_client: Any = None,
        codegraph_client: Any = None,
    ) -> None:
        self.engine = engine
        self.nats_publisher = nats_publisher
        self.llm_client = llm_client
        self.codegraph_client = codegraph_client
        self.config = OrchestratorConfig()
        self.conflict_detector = ConflictDetector()
        self.event_emitter: TaskEventEmitter | Any = TaskEventEmitter()

        # Task + worker state
        self.tasks: dict[str, Task] = {}
        self.workers: dict[str, WorkerEntry] = {}
        self._pending_task_count: int = 0

        # ponytail: scheduler stub — nats_worker checks orch.scheduler is not None
        self.scheduler = None

    # ── Worker registration ────────────────────────────────────

    async def register_worker(self, worker_info: WorkerInfo) -> str:
        """Register a worker. Returns worker ID."""
        entry = WorkerEntry(
            id=worker_info.id,
            capabilities=worker_info.capabilities,
            max_capacity=worker_info.max_capacity,
        )
        self.workers[worker_info.id] = entry
        logger.info("Registered worker: %s", worker_info.id)
        return worker_info.id

    def refresh_heartbeat(self, worker_id: str) -> None:
        """Update a worker's heartbeat timestamp."""
        entry = self.workers.get(worker_id)
        if entry:
            entry.last_heartbeat = datetime.now(timezone.utc)

    # ── Task submission ────────────────────────────────────────

    async def submit_task(
        self,
        description: str,
        project_id: str = "",
        task_id: str | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> Task:
        """Submit a task with simple newline-split decomposition.

        The full Orchestrator used LLM decomposition; this minimal version
        splits by newlines (same as mock mode).

        Args:
            description: Task description (newline-separated subtasks).
            project_id: Project identifier.
            task_id: Optional explicit task ID.
            agent_config: Per-subtask agent config overrides (applied to all subtasks).
        """
        tid = task_id or f"t-{uuid.uuid4().hex[:8]}"
        lines = [line.strip() for line in description.split("\n") if line.strip()]
        if not lines:
            lines = [description]

        subtasks = []
        for i, line in enumerate(lines):
            subtasks.append(Subtask(
                id=f"{tid}-s{i}",
                parent_id=tid,
                description=line,
                status=SubtaskStatus.PENDING,
                depends_on=[] if i == 0 else [],  # ponytail: no deps for simple split
                agent_config=agent_config or {},
            ))

        task = Task(
            id=tid,
            description=description,
            project_id=project_id,
            status=TaskStatus.IN_PROGRESS,
            subtasks=subtasks,
        )
        self.tasks[tid] = task
        logger.info("Task %s submitted (%d subtasks)", tid, len(subtasks))
        return task

    # ── Subtask lifecycle ──────────────────────────────────────

    async def assign_subtask(self, subtask: Subtask, worker_id: str) -> str | None:
        """Assign a subtask to a worker."""
        task = self.tasks.get(subtask.parent_id)
        if task is None or subtask.id not in {st.id for st in task.subtasks}:
            return None
        subtask.status = SubtaskStatus.ASSIGNED
        subtask.assigned_worker = worker_id
        # Update worker load
        entry = self.workers.get(worker_id)
        if entry:
            entry.current_load += 1
        return worker_id

    async def handle_subtask_result(self, result: SubtaskResult) -> None:
        """Process a subtask result — update task state."""
        for task in self.tasks.values():
            for st in task.subtasks:
                if st.id == result.subtask_id:
                    st.status = SubtaskStatus.COMPLETED if result.success else SubtaskStatus.FAILED
                    st.result = result
                    # Update worker load
                    wid = result.worker_id
                    entry = self.workers.get(wid)
                    if entry and entry.current_load > 0:
                        entry.current_load -= 1
                    # Check if all subtasks done
                    self._update_task_status(task)
                    return

    def _update_task_status(self, task: Task) -> None:
        """Update task status based on subtask states."""
        if all(st.status == SubtaskStatus.COMPLETED for st in task.subtasks):
            task.status = TaskStatus.COMPLETED
        elif any(st.status == SubtaskStatus.FAILED for st in task.subtasks):
            done_statuses = (SubtaskStatus.COMPLETED, SubtaskStatus.FAILED)
            if all(st.status in done_statuses for st in task.subtasks):
                task.status = TaskStatus.FAILED

    # ── Task queries ───────────────────────────────────────────

    def get_task_status(self, task_id: str) -> Task | None:
        """Get current task state."""
        return self.tasks.get(task_id)

    def select_next_subtask(
        self,
        task: Task,
        worker_capabilities: list[str] | None = None,
    ) -> Subtask | None:
        """Select the next ready (pending, deps met) subtask.

        If worker_capabilities is provided, only return subtasks whose
        required_capabilities are a subset of worker_capabilities (ALL match).
        """
        completed_ids = {st.id for st in task.subtasks if st.status == SubtaskStatus.COMPLETED}
        worker_caps = set(worker_capabilities) if worker_capabilities else None
        for st in task.subtasks:
            if st.status != SubtaskStatus.PENDING:
                continue
            # Check dependencies
            if not all(dep in completed_ids for dep in st.depends_on):
                continue
            # Check capabilities
            if worker_caps is not None and st.required_capabilities:
                if not set(st.required_capabilities).issubset(worker_caps):
                    continue
            return st
        return None

    # ── Task control ───────────────────────────────────────────

    def pause_task_local(self, task_id: str) -> None:
        """Pause a task (from NATS event)."""
        task = self.tasks.get(task_id)
        if task and task.status == TaskStatus.IN_PROGRESS:
            task.status = TaskStatus.PAUSED

    def resume_task_local(self, task_id: str) -> None:
        """Resume a paused task (from NATS event)."""
        task = self.tasks.get(task_id)
        if task and task.status == TaskStatus.PAUSED:
            task.status = TaskStatus.IN_PROGRESS

    async def cancel_task(self, task_id: str, subtask_id: str | None = None) -> bool:
        """Cancel a task or specific subtask."""
        task = self.tasks.get(task_id)
        if not task:
            return False
        if subtask_id:
            for st in task.subtasks:
                if st.id == subtask_id:
                    st.status = SubtaskStatus.FAILED
                    return True
            return False
        task.status = TaskStatus.FAILED
        return True

    # ── Convenience properties ─────────────────────────────────

    @property
    def pending_task_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status == TaskStatus.CREATED)

    async def flush_pending_tasks(self) -> list[Task]:
        """Flush pending tasks — stub for nats_worker dashboard handler."""
        return []
