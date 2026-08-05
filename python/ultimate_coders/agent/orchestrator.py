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
- LLM-based task decomposition (when llm_client available; newline-split fallback)

NOT provided (was in full Orchestrator, now handled by OMP extension):
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

    Provides just enough state management to run tasks through Workers.
    LLM decomposition is wired via ``llm_client`` (stored at init);
    when unavailable or on failure, falls back to newline-split.
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

        # Night-window exclusive mode (scheduler-spec.md §"Orchestrator
        # Night-Window Exclusive Mode"). When True, real-time (non-scheduled)
        # task submissions defer to ``_pending_tasks`` instead of immediate
        # decomposition/dispatch; scheduled tasks bypass the queue. Driven by
        # NATS ``schedule.window.opened``/``closed`` events (see nats_worker).
        self._night_window_active: bool = False
        self._pending_tasks: list[dict[str, Any]] = []

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
        _scheduled: bool = False,
    ) -> Task:
        """Submit a task with LLM decomposition (newline-split fallback).

        When ``self.llm_client`` is available, the description is sent to
        the LLM for decomposition into ordered subtasks (with dependencies,
        file constraints, and expected output). The LLM's JSON output is
        parsed by ``parse_decomposition_output`` (reused from sandbox.py).

        If ``llm_client`` is None, the LLM call raises, or parsing fails,
        the task falls back to newline-split decomposition (graceful,
        non-fatal — the task still runs).

        Night-window exclusive mode: when ``night_window_active`` is True
        and this submission is NOT scheduler-fired (``_scheduled=False``),
        the task is deferred to ``_pending_tasks`` with status PAUSED
        instead of being decomposed/dispatched immediately. Scheduled tasks
        (``_scheduled=True``) bypass the deferral and execute normally.
        See scheduler-spec.md §"Orchestrator Night-Window Exclusive Mode".

        Args:
            description: Task description (natural language or newline-separated).
            project_id: Project identifier.
            task_id: Optional explicit task ID.
            agent_config: Per-subtask agent config overrides (applied to all subtasks).
            _scheduled: Internal — True if this submit originated from the
                scheduler (cron/one-shot fire). Must NEVER be set by external
                callers; only the scheduler dispatch path sets it. Setting it
                incorrectly will bypass the night-window queue for real-time
                tasks.
        """
        # Night-window exclusive mode: defer real-time tasks to the pending
        # backlog. Scheduled tasks (from NatsSubmitDispatcher) bypass.
        if self._night_window_active and not _scheduled:
            tid = task_id or f"t-{uuid.uuid4().hex[:8]}"
            task = Task(
                id=tid,
                description=description,
                project_id=project_id,
                status=TaskStatus.PAUSED,
                subtasks=[],
            )
            self.tasks[tid] = task
            self._pending_tasks.append(
                {
                    "task_id": tid,
                    "description": description,
                    "project_id": project_id,
                    "agent_config": agent_config,
                }
            )
            logger.info(
                "Task %s deferred to _pending_tasks (night window active, "
                "real-time submit) — backlog size=%d",
                tid,
                len(self._pending_tasks),
            )
            return task

        tid = task_id or f"t-{uuid.uuid4().hex[:8]}"

        # Try LLM decomposition first; fall back to newline-split on any
        # failure (llm_client None, complete() raises, parse fails, empty).
        subtasks: list[Subtask] | None = None
        try:
            subtasks = await self._decompose_task(description, tid, project_id, agent_config)
        except Exception:
            logger.exception(
                "LLM decomposition failed for task %s, falling back to newline-split",
                tid,
            )
            subtasks = None

        if not subtasks:
            subtasks = self._newline_split_subtasks(description, tid, project_id, agent_config)

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

    # ── Decomposition ─────────────────────────────────────────

    #: System prompt for LLM decomposition. Instructs the LLM to output a
    #: JSON array matching the schema that ``parse_decomposition_output``
    #: (sandbox.py:689) expects.
    _DECOMPOSE_SYSTEM = (
        "You are a task decomposition agent. Break the given task into "
        "ordered subtasks that can be executed independently (or with "
        "dependencies). Output ONLY a JSON array — no prose, no markdown "
        "fences.\n\n"
        "Each element must be an object with these keys:\n"
        '  "description": string — a clear, self-contained subtask description\n'
        '  "depends_on": array of integers — 1-based indices of subtasks '
        "this one depends on (empty array if none)\n"
        '  "file_constraints": array of strings — file paths the subtask '
        "may read or modify (empty array if unknown)\n"
        '  "expected_output": string — what a successful result looks like\n'
        "\nExample:\n"
        '[\n'
        '  {"description": "Add foo() to bar.py", "depends_on": [], '
        '"file_constraints": ["bar.py"], "expected_output": "foo() defined"},\n'
        '  {"description": "Call foo() from main", "depends_on": [1], '
        '"file_constraints": ["main.py"], "expected_output": "main calls foo()"}\n'
        "]"
    )

    async def _decompose_task(
        self,
        description: str,
        task_id: str,
        project_id: str,
        agent_config: dict[str, Any] | None,
    ) -> list[Subtask] | None:
        """Decompose a task description via LLM into Subtask objects.

        Returns ``None`` if ``llm_client`` is not available or the LLM
        returns no usable subtasks. Any exception (network error, parse
        failure) propagates to the caller (``submit_task``), which catches
        it and falls back to newline-split.

        The returned subtasks have:
        - ``id``: ``"{task_id}-s{i}"`` (0-based index)
        - ``depends_on``: list of ``"{task_id}-s{idx-1}"`` (converting
          1-based LLM indices to 0-based subtask IDs)
        - ``file_constraints`` and ``expected_output`` from the LLM output
        """
        if self.llm_client is None:
            return None

        prompt = (
            f"Decompose this task into ordered subtasks as a JSON array.\n"
            f"Output ONLY the JSON array, no prose.\n\n"
            f"Task: {description}"
        )
        result = await self.llm_client.complete(
            messages=[{"role": "user", "content": prompt}],
            system=self._DECOMPOSE_SYSTEM,
            max_tokens=2048,
        )

        # LLMClient.complete returns LLMResponse (has .text). Be defensive
        # — duck-type for .text, fall back to str() for unknown shapes.
        if hasattr(result, "text"):
            raw_text = result.text
        elif isinstance(result, str):
            raw_text = result
        else:
            raw_text = str(result) if result else ""

        if not raw_text.strip():
            logger.warning("LLM decomposition returned empty text for task %s", task_id)
            return None

        from ultimate_coders.agent.sandbox import parse_decomposition_output

        items = parse_decomposition_output(raw_text)
        if not items:
            logger.warning("LLM decomposition produced 0 subtasks for task %s", task_id)
            return None

        subtasks: list[Subtask] = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            desc = item.get("description", "").strip()
            if not desc:
                continue
            # Convert 1-based depends_on indices to subtask IDs.
            raw_deps = item.get("depends_on", [])
            depends_on: list[str] = []
            if isinstance(raw_deps, list):
                for dep in raw_deps:
                    try:
                        idx = int(dep) - 1  # 1-based → 0-based
                        if 0 <= idx < len(items):
                            depends_on.append(f"{task_id}-s{idx}")
                    except (ValueError, TypeError):
                        logger.debug(
                            "Ignoring invalid depends_on entry %r in subtask %d",
                            dep, i,
                        )
            subtasks.append(Subtask(
                id=f"{task_id}-s{i}",
                parent_id=task_id,
                description=desc,
                status=SubtaskStatus.PENDING,
                depends_on=depends_on,
                file_constraints=item.get("file_constraints", []) or [],
                expected_output=item.get("expected_output", "") or "",
                agent_config=agent_config or {},
                project_id=project_id,
            ))

        if not subtasks:
            logger.warning(
                "LLM decomposition produced no valid subtasks for task %s "
                "(all items missing description)", task_id,
            )
            return None

        logger.info(
            "LLM decomposition for task %s: %d subtasks",
            task_id, len(subtasks),
        )
        return subtasks

    @staticmethod
    def _newline_split_subtasks(
        description: str,
        task_id: str,
        project_id: str,
        agent_config: dict[str, Any] | None,
    ) -> list[Subtask]:
        """Fallback: split description by newlines into subtasks."""
        lines = [line.strip() for line in description.split("\n") if line.strip()]
        if not lines:
            lines = [description]
        return [
            Subtask(
                id=f"{task_id}-s{i}",
                parent_id=task_id,
                description=line,
                status=SubtaskStatus.PENDING,
                depends_on=[],  # no deps for simple split
                agent_config=agent_config or {},
                project_id=project_id,
            )
            for i, line in enumerate(lines)
        ]

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

    # ── Night-window exclusive mode ─────────────────────────────

    @property
    def night_window_active(self) -> bool:
        """Whether the night window is active (exclusive mode).

        When True, real-time (non-scheduled) task submissions defer to
        ``_pending_tasks``; scheduled tasks bypass the queue. Driven by
        NATS ``schedule.window.opened``/``closed`` events.
        """
        return self._night_window_active

    def set_night_window_active(self, active: bool) -> None:
        """Toggle night-window exclusive mode.

        Called by nats_worker on ``schedule.window.opened`` (True) /
        ``schedule.window.closed`` (False) events. The ``closed`` handler
        also calls ``flush_pending_tasks()`` to drain the backlog.

        Args:
            active: True to enter exclusive mode (defer real-time tasks);
                False to exit (real-time tasks execute normally).
        """
        if self._night_window_active == active:
            return
        self._night_window_active = active
        logger.info(
            "Night window %s (exclusive mode %s, pending backlog=%d)",
            "opened" if active else "closed",
            "active" if active else "inactive",
            len(self._pending_tasks),
        )

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
        """Flush the night-window pending backlog.

        Drains ``_pending_tasks`` — each deferred task is re-submitted via
        the normal ``submit_task`` path (decomposition + dispatch), with
        ``_scheduled=False`` (they are real-time tasks that were deferred).
        The list is cleared before re-submission so a re-entrant defer (if
        the window reopens mid-flush) starts fresh.

        Called by nats_worker on ``schedule.window.closed`` (after setting
        ``night_window_active=False``) and by the dashboard
        ``flushpendingtasks`` RPC.

        Returns:
            The list of re-submitted Task objects (may be shorter than the
            backlog if some fail to decompose).
        """
        if not self._pending_tasks:
            return []
        backlog = self._pending_tasks
        self._pending_tasks = []
        logger.info("Flushing %d deferred task(s) from _pending_tasks", len(backlog))
        executed: list[Task] = []
        for entry in backlog:
            try:
                task = await self.submit_task(
                    entry["description"],
                    project_id=entry["project_id"],
                    task_id=entry["task_id"],
                    agent_config=entry.get("agent_config"),
                    _scheduled=False,
                )
                executed.append(task)
            except Exception:
                logger.exception(
                    "Failed to re-submit deferred task %s during flush",
                    entry.get("task_id"),
                )
        logger.info("Flushed %d/%d deferred task(s)", len(executed), len(backlog))
        return executed
