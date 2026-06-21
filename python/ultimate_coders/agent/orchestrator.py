"""Orchestrator — decomposes tasks into subtasks and coordinates Workers.

Uses sandbox (Claude Code) to:
1. Analyze the user's task
2. Decompose into subtasks with dependencies (DAG)
3. Assign subtasks to available workers
4. Monitor progress and handle failures
5. Aggregate results
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictResolver,
    ConflictResult,
    LineRange,
    ResolutionTier,
)
from ultimate_coders.agent.types import (
    OrchestratorConfig,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkerInfo,
)

logger = logging.getLogger(__name__)

# System prompt for task decomposition
_DECOMPOSE_SYSTEM_PROMPT = """\
You are a task decomposition assistant for a multi-agent coding system.

Given a user's coding task, decompose it into concrete, actionable subtasks.
Each subtask should be independently executable by a single worker agent.

Output a JSON array of subtask objects with these fields:
- "description": What the subtask should accomplish (be specific about files, functions, etc.)
- "depends_on": List of 1-based indices of subtasks this depends on (empty if none)
- "file_constraints": List of file paths that should NOT be modified
- "expected_output": What the completed subtask should produce

Guidelines:
- Keep subtasks small and focused (one logical change per subtask)
- Max {max_subtasks} subtasks
- Order subtasks so earlier ones provide context for later ones
- Make dependencies minimal but correct (don't create unnecessary serialization)
- Include a "research" subtask first if the task requires understanding existing code
- Include a "test" subtask if the task involves writing code

Respond with ONLY the JSON array, no other text.
"""

_DECOMPOSE_USER_TEMPLATE = """\
Project: {project_id}
Task: {description}
"""


class Orchestrator:
    """Decomposes tasks into subtasks and coordinates Workers.

    The Orchestrator is the central coordinator in the Orchestrator-Worker
    pattern. It receives user tasks, uses sandbox (Claude Code) to decompose
    them into subtasks with a dependency DAG, assigns subtasks to workers,
    and monitors progress.

    Usage:
        orchestrator = Orchestrator(engine=engine, sandbox_manager=sandbox)
        task = await orchestrator.submit_task("Implement user auth", project_id="my-app")
        # Workers execute subtasks...
        status = await orchestrator.get_task_status(task.id)
    """

    def __init__(
        self,
        engine: Any = None,
        config: OrchestratorConfig | None = None,
        conflict_detector: ConflictDetector | None = None,
        scheduler: Any = None,
        sandbox_manager: Any = None,
        nats_publisher: Any | None = None,
    ):
        """Initialize the Orchestrator.

        Args:
            engine: Engine instance for memory/search operations.
            config: Orchestrator configuration.
            conflict_detector: Conflict detector for edit intent tracking.
            scheduler: Optional Scheduler instance for scheduling tasks
                via the night-window orchestration system.
            sandbox_manager: SandboxManager for Claude Code-based
                decomposition (required for task decomposition).
            nats_publisher: Optional NatsPublisher for publishing task
                state changes to NATS. When set, the Orchestrator
                publishes ``uc.task.update`` and ``uc.task.event``
                messages after each state transition.
        """
        self.engine = engine
        self.sandbox_manager = sandbox_manager
        self.config = config or OrchestratorConfig()
        self.workers: dict[str, WorkerInfo] = {}
        self.tasks: dict[str, Task] = {}
        # Reverse index: subtask_id -> (task_id, index_in_subtasks)
        self._subtask_index: dict[str, tuple[str, int]] = {}
        self.conflict_detector = conflict_detector or ConflictDetector()
        # Night-window exclusive mode
        self.scheduler = scheduler
        self._night_window_active: bool = False
        self._pending_tasks: list[Task] = []
        # NATS publisher for state change events
        self.nats_publisher = nats_publisher
        # Event emitter for real-time dashboard tracking
        from ultimate_coders.agent.event_emitter import TaskEventEmitter

        self.event_emitter = TaskEventEmitter()

    async def submit_task(
        self,
        description: str,
        project_id: str = "",
        _scheduled: bool = False,
        task_id: str | None = None,
    ) -> Task:
        """Submit a new task for orchestration.

        Creates the task, decomposes it into subtasks via LLM,
        and schedules ready subtasks.

        When the night window is active (night_window_active=True),
        real-time (non-scheduled) tasks are queued instead of
        immediately executed. Scheduled tasks (from the scheduler)
        bypass the queue and execute immediately, giving them
        exclusive access to Worker resources.

        Args:
            description: The task description.
            project_id: Project/repository context (default: empty string).
            _scheduled: Internal flag — True if this task comes from the
                scheduler and should bypass the night-window queue.
            task_id: Optional task ID. When provided (e.g., from a NATS
                submit message), the Task object uses this ID instead of
                generating a new UUID. This ensures the caller's task_id
                matches the one stored in the Orchestrator.

        Returns:
            The created Task with subtasks.
        """
        # Night-window exclusive mode: queue non-scheduled tasks
        if self._night_window_active and not _scheduled:
            task = Task(
                id=task_id or "",
                description=description,
                project_id=project_id,
                status=TaskStatus.PAUSED,
            )
            self._pending_tasks.append(task)
            self.tasks[task.id] = task
            logger.info(
                "Task %s queued (night window active): %s",
                task.id,
                description,
            )
            return task

        # Create task
        task = Task(
            id=task_id or "",
            description=description,
            project_id=project_id,
            status=TaskStatus.PLANNING,
        )
        self.tasks[task.id] = task

        # Note: we intentionally do NOT call engine.submit_task() here.
        # When NATS is available, the gRPC server already created the Task
        # via submit_task_pending() and the Orchestrator's publish_update()
        # will sync state back. When NATS is unavailable (local path),
        # engine.submit_task() generates a new TaskId that mismatches the
        # one we just created above — causing a split-brain between the
        # Python and Rust task stores.  The NATS update path is the sole
        # mechanism for keeping TaskStore in sync.

        # Store in memory
        if self.engine is not None:
            try:
                self.engine.write_memory(
                    key_scope="task",
                    key="task_definition",
                    content=description,
                    content_type="text",
                    source_agent="orchestrator",
                    task_id=task.id,
                    project_id=project_id,
                )
            except Exception:
                logger.warning("Failed to write task to memory", exc_info=True)

        # Decompose into subtasks
        try:
            subtasks = await self.decompose_task(task)
            task.subtasks = subtasks
            self._rebuild_subtask_index(task)
            task.status = TaskStatus.IN_PROGRESS
        except Exception:
            logger.error("Failed to decompose task %s", task.id, exc_info=True)
            task.status = TaskStatus.FAILED
            task.result = "Failed to decompose task"

        # Emit task lifecycle event (NATS when available, local emitter otherwise)
        await self._publish_event(
            "task_submitted",
            task_id=task.id,
            data={
                "description": description,
                "project_id": project_id,
                "status": task.status.value,
                "subtask_count": len(task.subtasks),
            },
        )

        # Publish task state update to NATS (if nats_publisher is configured)
        if self.nats_publisher is not None:
            await self.nats_publisher.publish_update(task)

        task.update_timestamp()
        return task

    async def decompose_task(self, task: Task) -> list[Subtask]:
        """Decompose a task into subtasks via sandbox (Claude Code).

        Invokes ``claude -p "decompose..."`` via the ``DecomposeAdapter``
        and parses the JSON output. The sandbox agent (Claude Code) can
        read files and understand the codebase on its own, so no
        pre-gathered context is injected into the prompt.

        Args:
            task: The task to decompose.

        Returns:
            List of Subtask objects with dependencies.

        Raises:
            RuntimeError: If sandbox_manager is not configured or
                decomposition fails.
        """
        if self.sandbox_manager is None:
            raise RuntimeError(
                "sandbox_manager is required for task decomposition"
            )

        from ultimate_coders.agent.sandbox import (
            DecomposeAdapter,
            parse_decomposition_output,
        )

        # Build the decomposition prompt (simplified — no pre-gathered context)
        system = _DECOMPOSE_SYSTEM_PROMPT.format(
            max_subtasks=self.config.max_subtasks,
        )
        user_msg = _DECOMPOSE_USER_TEMPLATE.format(
            project_id=task.project_id or "unknown",
            description=task.description,
        )

        # Combine system + user into a single prompt for `claude -p`
        combined_prompt = f"{system}\n\n{user_msg}"
        logger.info(
            "Decomposing task %s via sandbox (prompt_len=%d)",
            task.id, len(combined_prompt),
        )
        adapter = DecomposeAdapter()
        request = adapter.build_request(
            combined_prompt,
            self.sandbox_manager.config.working_dir
            or self.sandbox_manager.config.project_path,
            self.sandbox_manager.config,
        )
        result = await self.sandbox_manager.execute_decompose(request)
        output = adapter.parse_output(result)
        if not output.success:
            logger.error(
                "Sandbox decomposition failed for task %s: %s",
                task.id, output.summary,
            )
            raise RuntimeError(f"Sandbox decomposition failed: {output.summary}")
        items = parse_decomposition_output(result.stdout)
        logger.info(
            "Sandbox decomposition succeeded for task %s: %d subtasks",
            task.id, len(items),
        )
        return self._parse_decomposition_items(items, task.id)

    async def assign_subtask(
        self,
        subtask: Subtask,
        worker_id: str | None = None,
    ) -> str | None:
        """Assign a subtask to a worker.

        If worker_id is specified, assigns directly. Otherwise, selects
        the best available worker based on capabilities and load.

        Args:
            subtask: The subtask to assign.
            worker_id: Optional specific worker to assign to.

        Returns:
            The worker ID the subtask was assigned to, or None if no
            suitable worker was found.
        """
        if worker_id is not None:
            worker = self.workers.get(worker_id)
            if worker is None:
                logger.warning("Worker %s not found", worker_id)
                return None
            if not worker.is_available:
                logger.warning("Worker %s is not available", worker_id)
                return None
        else:
            worker_id = self._select_worker(subtask)
            if worker_id is None:
                logger.warning("No available worker for subtask %s", subtask.id)
                return None
            worker = self.workers[worker_id]

        # Update state
        subtask.assigned_worker = worker_id
        subtask.status = SubtaskStatus.ASSIGNED
        worker.current_load += 1

        # Store assignment in memory
        if self.engine is not None:
            try:
                self.engine.write_memory(
                    key_scope="task",
                    key=f"assignment_{subtask.id}",
                    content=json.dumps(
                        {
                            "subtask_id": subtask.id,
                            "worker_id": worker_id,
                            "assigned_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    content_type="structured",
                    source_agent="orchestrator",
                    task_id=subtask.parent_id,
                    project_id=None,
                )
            except Exception:
                logger.warning("Failed to write assignment to memory", exc_info=True)

        logger.info(
            "Assigned subtask %s to worker %s",
            subtask.id,
            worker_id,
        )

        # Publish subtask assignment to NATS (if nats_publisher is configured)
        if self.nats_publisher is not None:
            # Find the parent task for the update
            parent_task = self.tasks.get(subtask.parent_id)
            if parent_task is not None:
                await self.nats_publisher.publish_update(parent_task)
            await self.nats_publisher.publish_event(
                "subtask_assigned",
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                data={"worker_id": worker_id},
            )

        return worker_id

    async def handle_subtask_result(self, result: SubtaskResult) -> None:
        """Handle a completed subtask result.

        Updates the task state, schedules newly ready subtasks,
        and checks if the overall task is complete.

        Args:
            result: The subtask result from a worker.
        """
        # Find the parent task and subtask via reverse index (O(1))
        entry = self._subtask_index.get(result.subtask_id)
        if entry is not None:
            task_id, subtask_idx = entry
            task = self.tasks.get(task_id)
            if task is not None and subtask_idx < len(task.subtasks):
                subtask = task.subtasks[subtask_idx]
                if subtask.id != result.subtask_id:
                    # Index out of sync (e.g. after re-decompose) — fall back
                    subtask = None
            else:
                subtask = None
        else:
            task = None
            subtask = None

        # Fallback: linear scan when index is empty or stale
        if subtask is None:
            for t in self.tasks.values():
                for st in t.subtasks:
                    if st.id == result.subtask_id:
                        task = t
                        subtask = st
                        break
                if subtask is not None:
                    break

        if task is None or subtask is None:
            logger.warning("No task found for subtask result %s", result.subtask_id)
            return

        # Update subtask state
        subtask.result = result
        if result.success:
            subtask.status = SubtaskStatus.COMPLETED
            logger.info("Subtask %s completed successfully", result.subtask_id)
        else:
            # Auto-retry: if retries remain, reset to PENDING instead of FAILED
            if subtask.retry_count < self.config.max_retries:
                subtask.retry_count += 1
                subtask.status = SubtaskStatus.PENDING
                subtask.assigned_worker = None
                logger.warning(
                    "Subtask %s failed (attempt %d/%d), will retry: %s",
                    result.subtask_id,
                    subtask.retry_count,
                    self.config.max_retries,
                    result.summary[:200],
                )
                # Emit retry event
                await self._publish_event(
                    "subtask_retrying",
                    task_id=task.id,
                    subtask_id=subtask.id,
                    data={
                        "attempt": subtask.retry_count,
                        "max_retries": self.config.max_retries,
                        "error": result.summary[:300],
                    },
                )
            else:
                subtask.status = SubtaskStatus.FAILED
                logger.warning(
                    "Subtask %s failed permanently (retries exhausted): %s",
                    result.subtask_id,
                    result.summary[:200],
                )

        # Release worker load
        if subtask.assigned_worker and subtask.assigned_worker in self.workers:
            worker = self.workers[subtask.assigned_worker]
            worker.current_load = max(0, worker.current_load - 1)

        # Store result in memory
        if self.engine is not None:
            try:
                self.engine.write_memory(
                    key_scope="task",
                    key=f"result_{result.subtask_id}",
                    content=result.summary,
                    content_type="text",
                    source_agent="orchestrator",
                    importance=0.7 if result.success else 0.9,
                    task_id=task.id,
                )
            except Exception:
                logger.warning("Failed to write result to memory", exc_info=True)

        # Check if overall task is complete
        task.update_timestamp()
        if task.is_complete:
            task.status = TaskStatus.COMPLETED
            task.result = self._aggregate_results(task)
            logger.info("Task %s completed", task.id)
            # Publish task completed event
            await self._publish_event(
                "task_completed",
                task_id=task.id,
                data={
                    "status": "completed",
                    "result_summary": (task.result or "")[:300],
                    "subtask_count": len(task.subtasks),
                    "completed_count": sum(1 for s in task.subtasks if s.is_complete),
                },
            )
            # Publish task state update to NATS
            if self.nats_publisher is not None:
                await self.nats_publisher.publish_update(task)
        elif task.has_failed:
            # Check if all subtasks are either completed or failed
            all_done = all(st.is_complete or st.is_failed for st in task.subtasks)
            if all_done:
                # Try dynamic re-decomposition before giving up
                redecomposed = await self._try_redecompose_failed(task)
                if redecomposed:
                    task.status = TaskStatus.IN_PROGRESS
                    logger.info(
                        "Task %s: re-decomposed %d failed subtask(s) into %d new ones",
                        task.id,
                        redecomposed["failed_count"],
                        redecomposed["new_count"],
                    )
                    await self._publish_event(
                        "task_redecomposed",
                        task_id=task.id,
                        data={
                            "new_subtask_count": redecomposed["new_count"],
                            "failed_replaced": redecomposed["failed_count"],
                        },
                    )
                else:
                    task.status = TaskStatus.FAILED
                    task.result = self._aggregate_results(task)
                    logger.warning("Task %s failed", task.id)
                    # Publish task failed event
                    await self._publish_event(
                        "task_completed",
                        task_id=task.id,
                        data={
                            "status": "failed",
                            "result_summary": (task.result or "")[:300],
                            "subtask_count": len(task.subtasks),
                            "failed_count": sum(1 for s in task.subtasks if s.is_failed),
                        },
                    )
                    # Publish task state update to NATS
                    if self.nats_publisher is not None:
                        await self.nats_publisher.publish_update(task)
        else:
            # Task still in progress — publish subtask status update
            if self.nats_publisher is not None:
                await self.nats_publisher.publish_update(task)
            event_type = "subtask_completed" if result.success else "subtask_failed"
            await self._publish_event(
                event_type,
                task_id=task.id,
                subtask_id=result.subtask_id,
                data={
                    "success": result.success,
                    "summary": result.summary[:300] if result.success else "",
                    "error": "" if result.success else result.summary[:300],
                },
            )

    async def register_worker(self, worker_info: WorkerInfo) -> None:
        """Register a new worker.

        Args:
            worker_info: Information about the worker.
        """
        self.workers[worker_info.id] = worker_info
        logger.info(
            "Registered worker %s with capabilities: %s",
            worker_info.id,
            worker_info.capabilities,
        )

    async def unregister_worker(self, worker_id: str) -> None:
        """Unregister a worker.

        Args:
            worker_id: ID of the worker to unregister.
        """
        if worker_id in self.workers:
            del self.workers[worker_id]
            logger.info("Unregistered worker %s", worker_id)

    async def get_task_status(self, task_id: str) -> Task | None:
        """Get current task status.

        Args:
            task_id: The task ID.

        Returns:
            The Task object, or None if not found.
        """
        return self.tasks.get(task_id)

    def get_available_workers(self) -> list[WorkerInfo]:
        """Get all currently available workers."""
        return [w for w in self.workers.values() if w.is_available]

    # ── Private helpers ─────────────────────────────────────────

    def _rebuild_subtask_index(self, task: Task) -> None:
        """Rebuild the subtask reverse index for a task.

        Called after subtasks are assigned (decompose / re-decompose).
        Removes stale entries for this task before rebuilding so that
        removed subtasks (e.g. failed ones replaced by re-decompose)
        don't linger in the index.
        """
        # Remove stale entries belonging to this task
        stale_ids = [
            sid for sid, (tid, _idx) in self._subtask_index.items()
            if tid == task.id
        ]
        for sid in stale_ids:
            del self._subtask_index[sid]
        # Rebuild from current subtask list
        for i, st in enumerate(task.subtasks):
            self._subtask_index[st.id] = (task.id, i)

    async def _publish_event(
        self,
        event_type: str,
        task_id: str = "",
        subtask_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Publish an event through the unified pipeline.

        When a NATS publisher is configured, events go exclusively to
        NATS (the Dashboard SSE and TUI consume from there). When NATS
        is unavailable (e.g. LocalWorker / JSON-RPC path), events fall
        back to the local event_emitter so they still reach the Rust
        gRPC server via ForwardingEventEmitter.
        """
        if self.nats_publisher is not None:
            await self.nats_publisher.publish_event(
                event_type, task_id=task_id, subtask_id=subtask_id, data=data,
            )
        else:
            await self.event_emitter.emit(
                event_type, task_id=task_id, subtask_id=subtask_id, data=data,
            )

    def _select_worker(self, subtask: Subtask) -> str | None:
        """Select the best available worker for a subtask.

        Strategy: prefer workers whose capabilities match the subtask
        description keywords, then pick by lowest load.

        Args:
            subtask: The subtask to assign.

        Returns:
            Worker ID, or None if no suitable worker found.
        """
        candidates = [w for w in self.workers.values() if w.is_available]

        if not candidates:
            return None

        # Extract keywords from subtask description for capability matching
        desc_lower = subtask.description.lower()
        cap_matches: dict[str, int] = {}
        for w in candidates:
            score = sum(1 for c in w.capabilities if c in desc_lower)
            cap_matches[w.id] = score

        # Sort: capability matches descending, then load ascending
        candidates.sort(key=lambda w: (-cap_matches[w.id], w.current_load, -w.max_capacity))
        return candidates[0].id

    def _select_next_subtask(self, task: Task) -> Subtask | None:
        """Select the next subtask to assign, respecting priority and dependencies.

        Candidates are subtasks that are PENDING and have all dependencies
        completed.  They are sorted by:
        1. Priority (higher value = higher priority, first)
        2. Dependency readiness (all deps completed)
        3. Round-robin among workers (via _select_worker tie-break)

        Args:
            task: The task whose subtasks to consider.

        Returns:
            The next Subtask to assign, or None if no subtask is ready.
        """
        ready = task.ready_subtasks
        if not ready:
            return None

        # Filter to only those whose dependencies are fully completed
        candidates = [st for st in ready if self._check_dependencies(st, task)]

        if not candidates:
            return None

        # Sort by priority descending (higher priority first)
        candidates.sort(key=lambda st: -st.priority)
        return candidates[0]

    def select_next_subtask(self, task: Task) -> Subtask | None:
        """Public API for selecting the next ready subtask.

        Delegates to _select_next_subtask. Use this instead of the
        private method from external callers (e.g., NatsWorker).
        """
        return self._select_next_subtask(task)

    def _check_dependencies(self, subtask: Subtask, task: Task) -> bool:
        """Check whether all dependencies of a subtask are completed.

        Args:
            subtask: The subtask whose dependencies to check.
            task: The parent task (used to look up dependency subtask status).

        Returns:
            True if all dependencies are completed (or there are none).
        """
        if not subtask.depends_on:
            return True

        subtask_map = {st.id: st for st in task.subtasks}
        for dep_id in subtask.depends_on:
            dep = subtask_map.get(dep_id)
            if dep is None or not dep.is_complete:
                return False
        return True

    def _parse_decomposition_items(
        self,
        items: list,
        parent_task_id: str,
    ) -> list[Subtask]:
        """Parse a list of subtask dicts into Subtask objects.

        Shared by both LLM and sandbox decomposition paths.

        Args:
            items: List of dicts with keys: description, depends_on,
                file_constraints, expected_output.
            parent_task_id: The parent task ID.

        Returns:
            List of Subtask objects.
        """
        # First pass: create subtasks with temporary index-based deps
        subtask_map: dict[int, Subtask] = {}
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            subtask = Subtask(
                parent_id=parent_task_id,
                description=item.get("description", f"Subtask {idx + 1}"),
                status=SubtaskStatus.PENDING,
                priority=item.get("priority", 0),
                file_constraints=item.get("file_constraints", []),
                expected_output=item.get("expected_output", ""),
            )
            subtask_map[idx] = subtask

        # Second pass: resolve dependencies from indices to IDs
        for idx, item in enumerate(items):
            if not isinstance(item, dict) or idx not in subtask_map:
                continue
            dep_indices = item.get("depends_on", [])
            subtask_map[idx].depends_on = [
                subtask_map[i].id for i in dep_indices if i in subtask_map
            ]

        return list(subtask_map.values())

    async def _gather_memory_context(self, task: Task) -> str:
        """Return project identifier for decomposition context.

        Simplified: the sandbox agent (Claude Code) reads files and
        understands the codebase on its own, so no pre-gathered memory
        search results are injected.
        """
        return task.project_id or ""

    async def _gather_code_context(self, task: Task) -> str:
        """Return project identifier for decomposition context.

        Simplified: the sandbox agent (Claude Code) reads files and
        understands the codebase on its own, so no pre-gathered code
        search results are injected.
        """
        return task.project_id or ""

    def _aggregate_results(self, task: Task) -> str:
        """Aggregate subtask results into a task result summary."""
        completed = [st for st in task.subtasks if st.is_complete]
        failed = [st for st in task.subtasks if st.is_failed]

        parts = []
        if completed:
            parts.append(f"Completed {len(completed)}/{len(task.subtasks)} subtasks:")
            for st in completed:
                summary = st.result.summary if st.result else "no details"
                parts.append(f"  - {st.description}: {summary}")

        if failed:
            parts.append(f"Failed {len(failed)} subtasks:")
            for st in failed:
                summary = st.result.summary if st.result else "no details"
                parts.append(f"  - {st.description}: {summary}")

        return "\n".join(parts)

    async def schedule_subtasks(
        self,
        task: Task,
        worker_execute: Any,
        max_concurrent: int = 4,
    ) -> list[SubtaskResult]:
        """Concurrently schedule and execute ready subtasks respecting the DAG.

        Analyzes the task's dependency DAG, identifies all currently ready
        subtasks (PENDING with all deps completed), assigns them to workers
        based on load and capabilities, and executes them concurrently.

        This is the main scheduling entry point for the Orchestrator-Worker
        pattern. It runs in rounds: each round schedules all ready subtasks,
        waits for them to complete, then checks for newly-ready subtasks.

        Includes stuck detection: if 2 consecutive rounds yield 0 new
        completions, the scheduler breaks and logs a warning.

        Args:
            task: The task whose subtasks to schedule.
            worker_execute: Async callable ``(worker_id, subtask) -> SubtaskResult``
                            that executes a subtask on a specific worker.
            max_concurrent: Maximum concurrent subtask executions (default 4).

        Returns:
            List of all SubtaskResults from this scheduling round.
        """
        import asyncio

        all_results: list[SubtaskResult] = []
        completed_before = sum(1 for st in task.subtasks if st.is_complete)
        consecutive_zero_progress = 0

        while True:
            # Find ready subtasks (PENDING, all deps completed)
            ready = self._get_ready_subtasks(task)
            if not ready:
                break

            # Assign to workers
            assignments: list[tuple[str, Subtask]] = []
            for subtask in ready[:max_concurrent]:
                worker_id = self._select_worker(subtask)
                if worker_id is None:
                    logger.warning(
                        "No available worker for subtask %s, will retry next round",
                        subtask.id,
                    )
                    continue
                assigned = await self.assign_subtask(subtask, worker_id)
                if assigned is not None:
                    assignments.append((worker_id, subtask))

            if not assignments:
                break  # no workers available or no assignable subtasks

            # Execute concurrently
            async def _run(
                wid: str, st: Subtask,
            ) -> SubtaskResult:
                try:
                    result = await worker_execute(wid, st)
                    await self.handle_subtask_result(result)
                    return result
                except Exception as e:
                    logger.error(
                        "Worker %s execution failed for subtask %s: %s",
                        wid, st.id, e, exc_info=True,
                    )
                    return SubtaskResult(
                        subtask_id=st.id,
                        worker_id=wid,
                        summary=f"Worker execution error: {e}",
                        success=False,
                    )

            results = await asyncio.gather(
                *[_run(wid, st) for wid, st in assignments],
            )
            all_results.extend(results)

            # Stuck detection: check progress since last round
            completed_now = sum(1 for st in task.subtasks if st.is_complete)
            new_progress = completed_now - completed_before
            completed_before = completed_now

            if new_progress == 0:
                consecutive_zero_progress += 1
                if consecutive_zero_progress >= 2:
                    logger.warning(
                        "Task %s stuck: 2 rounds with 0 progress, breaking scheduler",
                        task.id,
                    )
                    break
            else:
                consecutive_zero_progress = 0

            # Publish scheduling round event
            await self._publish_event(
                "scheduling_round_complete",
                task_id=task.id,
                data={
                    "round_count": len(assignments),
                    "new_progress": new_progress,
                    "total_completed": completed_now,
                    "total_failed": sum(1 for st in task.subtasks if st.is_failed),
                },
            )

        return all_results

    def _get_ready_subtasks(self, task: Task) -> list[Subtask]:
        """Get subtasks that are PENDING and have all dependencies completed.

        Args:
            task: The task whose subtasks to check.

        Returns:
            List of ready Subtask objects.
        """
        completed_ids = {st.id for st in task.subtasks if st.is_complete}
        return [
            st for st in task.subtasks
            if st.status == SubtaskStatus.PENDING
            and all(dep in completed_ids for dep in st.depends_on)
        ]

    async def _try_redecompose_failed(
        self, task: Task,
    ) -> dict[str, int] | None:
        """Try to re-decompose permanently failed subtasks into smaller ones.

        Only attempts if a sandbox_manager is available and there are failed
        subtasks that haven't been redecomposed before.

        Returns dict with failed_count and new_count on success, None on failure.
        """
        failed = [st for st in task.subtasks if st.is_failed]
        if not failed:
            return None

        # Only redecompose once per task to avoid infinite loops
        if any(st.retry_count > self.config.max_retries for st in failed):
            # Already retried and redecomposed — give up
            return None

        if self.sandbox_manager is None:
            return None

        # Build context from completed subtasks
        completed_summaries = []
        for st in task.subtasks:
            if st.is_complete and st.result:
                completed_summaries.append(f"- {st.description}: {st.result.summary[:200]}")

        failed_descriptions = []
        for st in failed:
            error = st.result.summary[:200] if st.result else "unknown error"
            failed_descriptions.append(f"- {st.description} (error: {error})")

        redecompose_prompt = (
            "The following subtasks of a larger task failed:\n"
            + "\n".join(failed_descriptions)
            + "\n\nCompleted subtasks so far:\n"
            + "\n".join(completed_summaries[:5] or ["(none)"])
            + "\n\nOriginal task: " + task.description
            + "\n\nDecompose each failed subtask into 1-2 simpler, more specific subtasks."
            + "\nOutput a JSON array with the same schema as before."
        )

        try:
            from ultimate_coders.agent.sandbox import (
                DecomposeAdapter,
                parse_decomposition_output,
            )
            adapter = DecomposeAdapter()
            request = adapter.build_request(
                redecompose_prompt,
                self.sandbox_manager.config.working_dir
                or self.sandbox_manager.config.project_path,
                self.sandbox_manager.config,
            )
            result = await self.sandbox_manager.execute_decompose(request)
            output = adapter.parse_output(result)
            if not output.success:
                return None
            items = parse_decomposition_output(result.stdout)

            if not isinstance(items, list) or not items:
                return None

            # Remove failed subtasks and add new ones
            new_subtasks = self._parse_decomposition_items(items, task.id)
            # Mark new subtasks as depending on all completed subtasks
            completed_ids = [st.id for st in task.subtasks if st.is_complete]
            for ns in new_subtasks:
                ns.depends_on = completed_ids.copy()
                ns.retry_count = self.config.max_retries  # prevent further retries

            # Replace failed subtasks with new ones
            task.subtasks = [
                st for st in task.subtasks if not st.is_failed
            ] + new_subtasks
            # Rebuild reverse index after subtask list changes
            self._rebuild_subtask_index(task)

            return {
                "failed_count": len(failed),
                "new_count": len(new_subtasks),
            }
        except Exception:
            logger.debug("Re-decomposition failed", exc_info=True)
            return None

    # ── Night-Window Exclusive Mode ─────────────────────────────────

    @property
    def night_window_active(self) -> bool:
        """Whether the night window is currently active.

        When active, real-time tasks are queued and only scheduled
        tasks (from the scheduler) are executed immediately.
        """
        return self._night_window_active

    def set_night_window_active(self, active: bool) -> None:
        """Set the night window active state.

        Called when the scheduler publishes a window.opened or
        window.closed event via NATS.

        Args:
            active: True if the night window is now open (scheduled
                tasks have exclusive access), False if it has closed.
        """
        self._night_window_active = active
        logger.info("Night window active: %s", active)

    async def flush_pending_tasks(self) -> list[Task]:
        """Execute all tasks that were queued during the night window.

        Called when the night window closes. Processes all deferred
        real-time tasks that were queued while scheduled tasks had
        exclusive access.

        Returns:
            List of tasks that were executed.
        """
        pending = self._pending_tasks.copy()
        self._pending_tasks.clear()
        executed: list[Task] = []

        for task in pending:
            logger.info(
                "Flushing pending task %s: %s",
                task.id,
                task.description,
            )
            # Re-submit the task for immediate execution
            # (night window is now closed, so it won't be re-queued)
            result = await self.submit_task(
                task.description,
                project_id=task.project_id,
            )
            executed.append(result)

        logger.info("Flushed %d pending tasks", len(executed))
        return executed

    @property
    def pending_task_count(self) -> int:
        """Number of tasks queued waiting for the night window to close."""
        return len(self._pending_tasks)

    # ── Scheduler Integration ──────────────────────────────────────

    def schedule_task(
        self,
        description: str,
        cron: str | None = None,
        execute_after: str | None = None,
        project_id: str | None = None,
        night_window_start: str | None = None,
        night_window_end: str | None = None,
        timezone: str = "UTC",
    ) -> Any:
        """Convenience method to schedule a task via the Scheduler.

        Requires that the Orchestrator was initialized with a scheduler.

        Args:
            description: Human-readable task description.
            cron: Cron expression for recurring tasks (e.g., "0 22 * * *").
            execute_after: ISO 8601 datetime for one-shot tasks.
            project_id: Project/repository context.
            night_window_start: Night window start time in HH:MM.
            night_window_end: Night window end time in HH:MM.
            timezone: IANA timezone name (default: "UTC").

        Returns:
            The ScheduledTask created by the scheduler.

        Raises:
            RuntimeError: If no scheduler is configured.
            ValueError: If neither cron nor execute_after is specified.
        """
        if self.scheduler is None:
            raise RuntimeError(
                "No scheduler configured. Pass scheduler= to Orchestrator.__init__()."
            )

        if cron is not None:
            return self.scheduler.create_cron_job(
                description,
                cron,
                project_id=project_id,
                night_window_start=night_window_start,
                night_window_end=night_window_end,
                timezone=timezone,
            )
        elif execute_after is not None:
            return self.scheduler.create_one_shot_job(
                description,
                execute_after,
                project_id=project_id,
                night_window_start=night_window_start,
                night_window_end=night_window_end,
                timezone=timezone,
            )
        else:
            raise ValueError(
                "Must specify either cron (for recurring) or execute_after (for one-shot)"
            )

    # ── Fault Tolerance Methods ───────────────────────────────────────

    async def checkpoint_task(self, task_id: str) -> str | None:
        """Create a checkpoint (snapshot) of a task's current state.

        Stores the task state in memory via the engine for recovery.
        Falls back to local serialization if engine doesn't support it.

        Args:
            task_id: The task ID to checkpoint.

        Returns:
            The snapshot ID, or None if checkpointing failed.
        """
        task = self.tasks.get(task_id)
        if task is None:
            logger.warning("Task %s not found for checkpoint", task_id)
            return None

        # Try engine checkpoint (if available)
        if self.engine is not None and hasattr(self.engine, "checkpoint_task"):
            try:
                snapshot_id = self.engine.checkpoint_task(task_id)
                logger.info("Created checkpoint for task %s: %s", task_id, snapshot_id)
                return snapshot_id
            except Exception:
                logger.warning("Engine checkpoint failed for %s", task_id, exc_info=True)

        # Fallback: serialize task state to engine memory
        if self.engine is not None:
            try:
                import json
                from datetime import datetime
                snapshot_id = f"snap-{task_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                self.engine.write_memory(
                    key_scope="task",
                    key=f"checkpoint_{snapshot_id}",
                    content=json.dumps(task.to_dict()),
                    content_type="structured",
                    source_agent="orchestrator",
                    task_id=task_id,
                )
                logger.info("Created memory checkpoint for task %s: %s", task_id, snapshot_id)
                return snapshot_id
            except Exception:
                logger.warning("Memory checkpoint failed for %s", task_id, exc_info=True)

        return None

    async def recover_task(self, task_id: str) -> dict | None:
        """Recover a task from the latest checkpoint.

        Tries the engine's recovery system first, then falls back to
        reading from engine memory.

        Args:
            task_id: The task ID to recover.

        Returns:
            The recovered task state dict, or None if recovery failed.
        """
        # Try engine recovery (if available)
        if self.engine is not None and hasattr(self.engine, "recover_task"):
            try:
                state = self.engine.recover_task(task_id)
                logger.info("Recovered task %s from engine", task_id)
                return state
            except Exception:
                logger.warning("Engine recovery failed for %s", task_id, exc_info=True)

        # Fallback: check if task exists in local dict
        task = self.tasks.get(task_id)
        if task is not None:
            logger.info("Recovered task %s from local state", task_id)
            return task.to_dict()

        return None

    def check_edit_conflict(
        self,
        worker_id: str,
        file_path: str,
        regions: list[tuple[int, int]],
    ) -> tuple[ConflictResult, dict | None]:
        """Check if a worker's edit would conflict with existing intents.

        Args:
            worker_id: The worker making the edit.
            file_path: Path to the file being edited.
            regions: List of (start, end) line range tuples.

        Returns:
            A tuple of (ConflictResult, optional conflict info dict).
        """
        line_ranges = [LineRange(start=s, end=e) for s, e in regions]
        result, info = self.conflict_detector.check_conflict(
            file_path,
            worker_id,
            line_ranges,
        )
        return result, info

    def resolve_conflict(
        self,
        base: str,
        ours: str,
        theirs: str,
        tier: ResolutionTier = ResolutionTier.AUTO_MERGE,
    ) -> dict:
        """Resolve a conflict using the resolution pipeline.

        Args:
            base: The original content.
            ours: Content from "ours" side.
            theirs: Content from "theirs" side.
            tier: The resolution tier to start with.

        Returns:
            A dict with 'success', 'merged', and 'conflicts' keys.
        """
        resolver = ConflictResolver()
        result = resolver.resolve(base, ours, theirs, tier)
        return {
            "success": result.success,
            "merged": result.merged,
            "conflicts": [
                {
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "ours": c.ours,
                    "theirs": c.theirs,
                    "base": c.base,
                }
                for c in result.conflicts
            ],
            "tier": result.tier.value,
        }

    def pause_task(self, task_id: str) -> bool:
        """Pause a running task.

        Sets the task status to PAUSED. Paused tasks will not have
        their subtasks assigned to workers until resumed.

        Args:
            task_id: The task ID to pause.

        Returns:
            True if the task was paused, False if not found or not pausable.
        """
        task = self.tasks.get(task_id)
        if task is None:
            logger.warning("Task %s not found for pause", task_id)
            return False
        if task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.PLANNING):
            logger.warning(
                "Task %s cannot be paused (current status: %s)",
                task_id,
                task.status.value,
            )
            return False
        task.status = TaskStatus.PAUSED
        task.update_timestamp()
        # Sync to Engine task store so gRPC consumers see the update
        if self.engine is not None:
            try:
                self.engine.pause_task(task_id)
            except Exception:
                logger.debug("Engine pause_task sync failed for %s", task_id, exc_info=True)
        logger.info("Task %s paused", task_id)
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a paused task.

        Sets the task status back to IN_PROGRESS.

        Args:
            task_id: The task ID to resume.

        Returns:
            True if the task was resumed, False if not found or not resumable.
        """
        task = self.tasks.get(task_id)
        if task is None:
            logger.warning("Task %s not found for resume", task_id)
            return False
        if task.status != TaskStatus.PAUSED:
            logger.warning(
                "Task %s cannot be resumed (current status: %s)",
                task_id,
                task.status.value,
            )
            return False
        task.status = TaskStatus.IN_PROGRESS
        task.update_timestamp()
        # Sync to Engine task store so gRPC consumers see the update
        if self.engine is not None:
            try:
                self.engine.resume_task(task_id)
            except Exception:
                logger.debug("Engine resume_task sync failed for %s", task_id, exc_info=True)
        logger.info("Task %s resumed", task_id)
        return True

    def pause_task_local(self, task_id: str) -> bool:
        """Pause a running task (local state only, no engine sync).

        Called when a ``task_paused`` event arrives from NATS (originated
        by the Rust gRPC server).  We only update local state here to
        avoid a feedback loop: Python→Rust→NATS→Python.

        Args:
            task_id: The task ID to pause.

        Returns:
            True if the task was paused, False if not found or not pausable.
        """
        task = self.tasks.get(task_id)
        if task is None:
            logger.warning("Task %s not found for local pause", task_id)
            return False
        if task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.PLANNING):
            logger.debug(
                "Task %s already not pausable (status: %s), ignoring",
                task_id,
                task.status.value,
            )
            return False
        task.status = TaskStatus.PAUSED
        task.update_timestamp()
        logger.info("Task %s paused (local, from NATS event)", task_id)
        return True

    def resume_task_local(self, task_id: str) -> bool:
        """Resume a paused task (local state only, no engine sync).

        Called when a ``task_resumed`` event arrives from NATS (originated
        by the Rust gRPC server).  We only update local state here to
        avoid a feedback loop.

        Args:
            task_id: The task ID to resume.

        Returns:
            True if the task was resumed, False if not found or not resumable.
        """
        task = self.tasks.get(task_id)
        if task is None:
            logger.warning("Task %s not found for local resume", task_id)
            return False
        if task.status != TaskStatus.PAUSED:
            logger.debug(
                "Task %s not paused (status: %s), ignoring resume",
                task_id,
                task.status.value,
            )
            return False
        task.status = TaskStatus.IN_PROGRESS
        task.update_timestamp()
        logger.info("Task %s resumed (local, from NATS event)", task_id)
        return True

    # ── Dashboard ──────────────────────────────────────────────────

    def start_dashboard(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start the embedded web dashboard for monitoring.

        Launches a FastAPI server in a background thread that serves
        the dashboard UI and SSE stream. The dashboard reads
        Orchestrator state directly from memory for zero-latency
        updates.

        When the Orchestrator has a NatsPublisher configured, it is
        passed to the Dashboard so that task submit/pause/resume are
        routed through NATS. The Dashboard also subscribes to
        ``uc.task.event`` for real-time event streaming.

        Args:
            host: Bind address (default: "0.0.0.0").
            port: Bind port (default: 8080).

        Raises:
            ImportError: If dashboard dependencies are not installed.
        """
        try:
            from ultimate_coders.dashboard.app import DashboardApp
        except ImportError as e:
            raise ImportError(
                "Dashboard dependencies not installed. "
                "Install with: pip install fastapi uvicorn jinja2 sse-starlette"
            ) from e

        if hasattr(self, "_dashboard_app") and self._dashboard_app is not None:
            logger.warning("Dashboard is already running")
            return

        self._dashboard_app = DashboardApp(
            self,
            nats_publisher=self.nats_publisher,
        )
        self._dashboard_app.start(host=host, port=port)

    def stop_dashboard(self) -> None:
        """Stop the embedded web dashboard.

        Gracefully shuts down the FastAPI server.
        """
        if hasattr(self, "_dashboard_app") and self._dashboard_app is not None:
            self._dashboard_app.stop()
            self._dashboard_app = None
        else:
            logger.warning("Dashboard is not running")
