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

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ultimate_coders.agent.aggregator import ResultAggregator
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
        merge_arbiter: Any = None,
    ) -> None:
        self.engine = engine
        self.nats_publisher = nats_publisher
        self.llm_client = llm_client
        self.codegraph_client = codegraph_client
        # Phase 2: git-level merge arbitration. Opt-in — only set when an
        # external remote is configured (UC_REPO_URL). When None, the
        # Orchestrator behaves exactly as before (local-only, no remote sync).
        self.merge_arbiter = merge_arbiter
        # Advisory in-memory result aggregator. Runs at task completion
        # (before MergeArbiter) to surface same-file conflicts between
        # concurrent subtasks early. Advisory only — does NOT write merged
        # content; MergeArbiter remains the writer. LLM synthesis is
        # out-of-scope (llm_client=None) per PRD.
        self.aggregator = ResultAggregator(llm_client=None)
        # Hold strong refs to fire-and-forget aggregation tasks so the event
        # loop's weak references don't GC them mid-flight (mirrors
        # _pending_arbitration).
        self._pending_aggregation: set[asyncio.Task[Any]] = set()
        # Hold strong refs to fire-and-forget arbitration tasks so the event
        # loop's weak references don't GC them mid-flight (CPython asyncio
        # warns/finalizes unreferenced tasks). Cleared as each completes.
        self._pending_arbitration: set[asyncio.Task[Any]] = set()
        self.config = OrchestratorConfig()
        self.conflict_detector = ConflictDetector()
        self.event_emitter: TaskEventEmitter | Any = TaskEventEmitter()

        # Task + worker state
        self.tasks: dict[str, Task] = {}
        self.workers: dict[str, WorkerEntry] = {}

        # Kept as None — Rust SchedulerService owns scheduling (see
        # crates/uc-engine/src/scheduler/). nats_worker + dashboard guard
        # on `orch.scheduler is None` and return available=False.
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
                project_id=project_id,
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
                    # ponytail: F55 — idempotent. A result can legitimately
                    # arrive twice: default-mode workers consume their own
                    # published uc.task.event loopback (audit #3), so the
                    # _run_one direct call and the loopback both land here.
                    # Double-processing used to decrement worker load twice
                    # (stealing capacity accounting from concurrent subtasks)
                    # and re-fire _schedule_arbitration — two concurrent
                    # MergeArbiter runs merging into main. First result wins;
                    # retries are unaffected because the subtask is reset to
                    # PENDING before re-execution (status leaves terminal).
                    if st.status in (
                        SubtaskStatus.COMPLETED, SubtaskStatus.FAILED,
                    ):
                        logger.debug(
                            "Subtask %s result already applied (%s); "
                            "ignoring duplicate",
                            st.id[:8], st.status.value,
                        )
                        return
                    st.status = SubtaskStatus.COMPLETED if result.success else SubtaskStatus.FAILED
                    st.result = result
                    # Update worker load
                    wid = result.worker_id
                    entry = self.workers.get(wid)
                    if entry and entry.current_load > 0:
                        entry.current_load -= 1
                    # Check if all subtasks done
                    self._update_task_status(task)
                    # Evict terminal tasks to bound memory on long-running workers.
                    self.evict_terminal_tasks()
                    return

    def _update_task_status(self, task: Task) -> None:
        """Update task status based on subtask states."""
        if all(st.status == SubtaskStatus.COMPLETED for st in task.subtasks):
            task.status = TaskStatus.COMPLETED
            # Advisory in-memory aggregation — surfaces same-file conflicts
            # between concurrent subtasks early (non-fatal, does NOT write).
            # Runs before MergeArbiter so conflicts are visible in logs
            # before git-level merge is attempted. Fire-and-forget via
            # asyncio.create_task so the sync _update_task_status path is
            # not blocked (mirrors _schedule_arbitration below).
            self._schedule_aggregation(task)
            # Phase 2: fire git-level merge arbitration when all subtasks
            # complete. Opt-in — only when a MergeArbiter is configured.
            # Non-fatal: arbitration failures are logged, never crash task
            # completion. Fire-and-forget via asyncio.create_task so the
            # sync _update_task_status path is not blocked.
            if self.merge_arbiter is not None:
                self._schedule_arbitration(task)
        elif any(st.status == SubtaskStatus.FAILED for st in task.subtasks):
            done_statuses = (SubtaskStatus.COMPLETED, SubtaskStatus.FAILED)
            if all(st.status in done_statuses for st in task.subtasks):
                task.status = TaskStatus.FAILED

    # Cap on retained tasks. Terminal tasks (Completed/Failed/Cancelled) beyond
    # this count are evicted to prevent unbounded growth on a long-running
    # worker. Mirrors the TypeScript orchestrator's evictCompletedTasks.
    MAX_RETAINED_TASKS: int = 200

    def evict_terminal_tasks(self) -> int:
        """Evict oldest terminal tasks when the map exceeds MAX_RETAINED_TASKS.

        Returns the number of tasks evicted.
        """
        if len(self.tasks) <= self.MAX_RETAINED_TASKS:
            return 0
        # Task has no completed_at; use created_at as the eviction ordering key.
        terminal = [
            (tid, t.created_at)
            for tid, t in self.tasks.items()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
        if not terminal:
            return 0
        # Evict oldest terminal tasks first (smallest created_at).
        terminal.sort(key=lambda item: item[1])
        excess = len(self.tasks) - self.MAX_RETAINED_TASKS
        to_evict = min(excess, len(terminal))
        for tid, _ in terminal[:to_evict]:
            del self.tasks[tid]
        return to_evict

    def _schedule_aggregation(self, task: Task) -> None:
        """Schedule advisory result aggregation as a background task.

        Collects ``SubtaskResult`` objects from completed subtasks, sources
        ``base_files`` from the MergeArbiter's project path (if available),
        and runs the ``ResultAggregator`` to surface same-file conflicts.
        Advisory only — does NOT write merged content; MergeArbiter remains
        the writer. Non-fatal: aggregation failures are logged, never crash
        task completion.

        Mirrors ``_schedule_arbitration``: fire-and-forget via
        ``asyncio.create_task`` so the sync ``_update_task_status`` path is
        not blocked.
        """
        # Collect results from subtasks that have one (completed subtasks).
        subtask_results = [
            st.result for st in task.subtasks
            if st.result is not None
        ]
        if not subtask_results:
            return
        try:
            agg_task = asyncio.create_task(
                self._aggregate_results(task.id, subtask_results)
            )
        except RuntimeError:
            # No running event loop — cannot schedule. Log and skip.
            logger.warning(
                "Cannot schedule result aggregation for task %s "
                "(no running event loop)", task.id,
            )
            return
        # Hold a strong reference so the task is not GC'd mid-flight.
        self._pending_aggregation.add(agg_task)
        agg_task.add_done_callback(self._pending_aggregation.discard)

    async def _aggregate_results(
        self,
        task_id: str,
        subtask_results: list[SubtaskResult],
    ) -> None:
        """Run advisory result aggregation for a completed task (non-fatal).

        Sources ``base_files`` from the MergeArbiter's project path if
        available; if no MergeArbiter or no project path, ``base_files``
        is empty (first-modifier-wins semantics). Logs the aggregation
        result — conflicts are warned about but never abort the task.
        Wrapped in try/except so aggregation failures never propagate.
        """
        try:
            base_files = self._collect_base_files(subtask_results)
            result = await self.aggregator.aggregate(
                subtask_results, base_files,
            )
            if result.conflict_files:
                logger.warning(
                    "Result aggregation for task %s: status=%s, "
                    "conflicts=%d (%s), merged=%d",
                    task_id, result.status.value,
                    len(result.conflict_files),
                    ", ".join(result.conflict_files),
                    len(result.merged_files),
                )
            else:
                logger.info(
                    "Result aggregation for task %s: status=%s, "
                    "merged=%d files",
                    task_id, result.status.value,
                    len(result.merged_files),
                )
        except Exception:
            logger.exception(
                "Result aggregation failed for task %s (non-fatal)",
                task_id,
            )

    def _collect_base_files(
        self, subtask_results: list[SubtaskResult],
    ) -> dict[str, str]:
        """Source original file contents for the three-way merge base.

        Reads from the MergeArbiter's project path (if configured). Only
        files that appear in the subtask results' ``modified_files`` are
        read — avoids reading the entire workspace. If no MergeArbiter or
        no project path, returns ``{}`` (empty base — first-modifier-wins).
        """
        if self.merge_arbiter is None:
            return {}
        project_path = getattr(self.merge_arbiter, "_project_path", None)
        if not project_path:
            return {}

        # Collect all modified file paths across all subtask results.
        modified_paths: set[str] = set()
        for sr in subtask_results:
            for fc in sr.modified_files:
                if fc.file_path:
                    modified_paths.add(fc.file_path)

        base_files: dict[str, str] = {}
        for fpath in modified_paths:
            full_path = os.path.join(project_path, fpath)
            try:
                with open(full_path, encoding="utf-8") as f:
                    base_files[fpath] = f.read()
            except (OSError, UnicodeDecodeError):
                # File may not exist yet (created by a subtask) or be
                # binary — skip it. An empty/missing base is acceptable
                # (first-modifier-wins).
                logger.debug(
                    "Could not read base for %s (skipping): %s",
                    fpath, full_path,
                )
        return base_files

    def _schedule_arbitration(self, task: Task) -> None:
        """Schedule merge arbitration as a background task (non-blocking).

        Collects the ``uc/subtask/<id>`` branch names from the task's
        subtasks (branch naming matches ``WorkspaceManager.acquire``:
        ``uc/subtask/{subtask_id[:12]}``).

        The created task is stored in ``self._pending_arbitration`` (a strong
        ref) so CPython's event loop cannot garbage-collect it before it
        completes. The coroutine removes itself from the set on exit.
        """
        branches = [
            f"uc/subtask/{st.id[:12]}" for st in task.subtasks
        ]
        try:
            arb_task = asyncio.create_task(
                self._arbitrate_task(task.id, branches)
            )
        except RuntimeError:
            # No running event loop — cannot schedule. Log and skip.
            logger.warning(
                "Cannot schedule merge arbitration for task %s "
                "(no running event loop)", task.id,
            )
            return
        # Hold a strong reference so the task is not GC'd mid-flight.
        self._pending_arbitration.add(arb_task)
        arb_task.add_done_callback(self._pending_arbitration.discard)

    async def _arbitrate_task(
        self, task_id: str, branches: list[str],
    ) -> None:
        """Run merge arbitration for a completed task (non-fatal).

        Wrapped in try/except so arbitration failures never propagate to
        the task-completion path.
        """
        if self.merge_arbiter is None:
            return
        try:
            logger.info(
                "Starting merge arbitration for task %s (%d branches)",
                task_id, len(branches),
            )
            result = await self.merge_arbiter.arbitrate(branches)
            logger.info(
                "Merge arbitration for task %s: status=%s, merged=%d, "
                "conflicts=%d, push=%s",
                task_id, result.get("status"),
                len(result.get("merged_branches", [])),
                len(result.get("conflict_branches", [])),
                result.get("push_status"),
            )
        except Exception:
            logger.exception(
                "Merge arbitration failed for task %s (non-fatal)", task_id,
            )

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
        # Tasks are submitted as IN_PROGRESS (never CREATED); "pending" means
        # active (not yet terminal). Count IN_PROGRESS + PAUSED.
        return sum(
            1
            for t in self.tasks.values()
            if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.PAUSED)
        )

    async def flush_pending_tasks(self) -> list[Task]:
        """Flush pending tasks — stub for nats_worker dashboard handler."""
        return []
