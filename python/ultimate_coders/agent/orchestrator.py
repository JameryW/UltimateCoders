"""Orchestrator — decomposes tasks into subtasks and coordinates Workers.

Uses LLM to:
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
from ultimate_coders.agent.llm import LLMClient, LLMResponse
from ultimate_coders.agent.rate_limiter import (
    CircuitBreaker,
    RateLimiter,
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

Context from memory:
{memory_context}

Relevant code context:
{code_context}
"""


class Orchestrator:
    """Decomposes tasks into subtasks and coordinates Workers.

    The Orchestrator is the central coordinator in the Orchestrator-Worker
    pattern. It receives user tasks, uses LLM to decompose them into
    subtasks with a dependency DAG, assigns subtasks to workers, and
    monitors progress.

    Usage:
        orchestrator = Orchestrator(engine=engine, llm_client=llm)
        task = await orchestrator.submit_task("Implement user auth", project_id="my-app")
        # Workers execute subtasks...
        status = await orchestrator.get_task_status(task.id)
    """

    def __init__(
        self,
        engine: Any = None,
        llm_client: LLMClient | None = None,
        config: OrchestratorConfig | None = None,
        conflict_detector: ConflictDetector | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        scheduler: Any = None,
        sandbox_manager: Any = None,
        nats_publisher: Any | None = None,
    ):
        """Initialize the Orchestrator.

        Args:
            engine: Engine instance for memory/search operations.
            llm_client: LLM client for task decomposition.
            config: Orchestrator configuration.
            conflict_detector: Conflict detector for edit intent tracking.
            rate_limiter: Rate limiter for LLM API calls.
            circuit_breaker: Circuit breaker for LLM API fault tolerance.
            scheduler: Optional Scheduler instance for scheduling tasks
                via the night-window orchestration system.
            sandbox_manager: Optional SandboxManager for Claude Code-based
                decomposition (used when llm_client is None).
            nats_publisher: Optional NatsPublisher for publishing task
                state changes to NATS. When set, the Orchestrator
                publishes ``uc.task.update`` and ``uc.task.event``
                messages after each state transition.
        """
        self.engine = engine
        self.llm_client = llm_client
        self.sandbox_manager = sandbox_manager
        self.config = config or OrchestratorConfig()
        self.workers: dict[str, WorkerInfo] = {}
        self.tasks: dict[str, Task] = {}
        self.conflict_detector = conflict_detector or ConflictDetector()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
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

        Returns:
            The created Task with subtasks.
        """
        # Night-window exclusive mode: queue non-scheduled tasks
        if self._night_window_active and not _scheduled:
            task = Task(
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
            description=description,
            project_id=project_id,
            status=TaskStatus.PLANNING,
        )
        self.tasks[task.id] = task

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
            task.status = TaskStatus.IN_PROGRESS
        except Exception:
            logger.error("Failed to decompose task %s", task.id, exc_info=True)
            task.status = TaskStatus.FAILED
            task.result = "Failed to decompose task"

        # Emit task lifecycle event
        await self.event_emitter.emit(
            "task_submitted",
            task_id=task.id,
            data={
                "description": description,
                "project_id": project_id,
                "status": task.status.value,
                "subtask_count": len(task.subtasks),
            },
        )

        # Publish task state to NATS (if nats_publisher is configured)
        if self.nats_publisher is not None:
            await self.nats_publisher.publish_update(task)
            await self.nats_publisher.publish_event(
                "task_submitted",
                task_id=task.id,
                data={
                    "description": description,
                    "project_id": project_id,
                    "status": task.status.value,
                    "subtask_count": len(task.subtasks),
                },
            )

        task.update_timestamp()
        return task

    async def decompose_task(self, task: Task) -> list[Subtask]:
        """Decompose a task into subtasks via LLM or sandbox (Claude Code).

        When ``self.llm_client`` is set, uses the traditional Python
        ``LLMClient.complete()`` path.  When ``self.sandbox_manager`` is
        set instead, invokes ``claude -p "decompose..."`` via the
        ``DecomposeAdapter`` and parses the JSON output.

        Args:
            task: The task to decompose.

        Returns:
            List of Subtask objects with dependencies.

        Raises:
            RuntimeError: If neither LLM client nor sandbox manager is
                configured, or if decomposition fails.
        """
        # Gather context from memory and search
        memory_context = await self._gather_memory_context(task)
        code_context = await self._gather_code_context(task)

        # Build the decomposition prompt (shared by both paths)
        system = _DECOMPOSE_SYSTEM_PROMPT.format(
            max_subtasks=self.config.max_subtasks,
        )
        user_msg = _DECOMPOSE_USER_TEMPLATE.format(
            project_id=task.project_id or "unknown",
            description=task.description,
            memory_context=memory_context,
            code_context=code_context,
        )

        # ── Sandbox (Claude Code) path ──
        if self.sandbox_manager is not None and self.llm_client is None:
            from ultimate_coders.agent.sandbox import (
                DecomposeAdapter,
                parse_decomposition_output,
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
            result = await self.sandbox_manager._execute_subprocess(request)
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

        # ── Traditional LLM path ──
        if self.llm_client is None:
            raise RuntimeError(
                "Either llm_client or sandbox_manager is required for task decomposition"
            )

        response = await self.llm_client.complete(
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            temperature=0.3,
            max_tokens=2048,
        )

        # Parse response into Subtask objects
        return self._parse_decomposition(response, task.id)

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
        # Find the parent task
        task = None
        for t in self.tasks.values():
            for st in t.subtasks:
                if st.id == result.subtask_id:
                    task = t
                    break
            if task is not None:
                break

        if task is None:
            logger.warning("No task found for subtask result %s", result.subtask_id)
            return

        # Find the subtask
        subtask = None
        for st in task.subtasks:
            if st.id == result.subtask_id:
                subtask = st
                break

        if subtask is None:
            logger.warning("Subtask %s not found", result.subtask_id)
            return

        # Update subtask state
        subtask.result = result
        if result.success:
            subtask.status = SubtaskStatus.COMPLETED
            logger.info("Subtask %s completed successfully", result.subtask_id)
        else:
            subtask.status = SubtaskStatus.FAILED
            logger.warning("Subtask %s failed: %s", result.subtask_id, result.summary)

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
            # Emit task completed event
            await self.event_emitter.emit(
                "task_completed",
                task_id=task.id,
                data={
                    "status": "completed",
                    "result_summary": (task.result or "")[:300],
                    "subtask_count": len(task.subtasks),
                    "completed_count": sum(1 for s in task.subtasks if s.is_complete),
                },
            )
            # Publish task completion to NATS
            if self.nats_publisher is not None:
                await self.nats_publisher.publish_update(task)
                await self.nats_publisher.publish_event(
                    "task_completed",
                    task_id=task.id,
                    data={
                        "status": "completed",
                        "result_summary": (task.result or "")[:300],
                    },
                )
        elif task.has_failed:
            # Check if all subtasks are either completed or failed
            all_done = all(st.is_complete or st.is_failed for st in task.subtasks)
            if all_done:
                task.status = TaskStatus.FAILED
                task.result = self._aggregate_results(task)
                logger.warning("Task %s failed", task.id)
                # Emit task completed (failed) event
                await self.event_emitter.emit(
                    "task_completed",
                    task_id=task.id,
                    data={
                        "status": "failed",
                        "result_summary": (task.result or "")[:300],
                        "subtask_count": len(task.subtasks),
                        "failed_count": sum(1 for s in task.subtasks if s.is_failed),
                    },
                )
                # Publish task failure to NATS
                if self.nats_publisher is not None:
                    await self.nats_publisher.publish_update(task)
                    await self.nats_publisher.publish_event(
                        "task_completed",
                        task_id=task.id,
                        data={
                            "status": "failed",
                            "result_summary": (task.result or "")[:300],
                        },
                    )
        else:
            # Task still in progress — publish subtask status update
            if self.nats_publisher is not None:
                await self.nats_publisher.publish_update(task)
                event_type = "subtask_completed" if result.success else "subtask_failed"
                await self.nats_publisher.publish_event(
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

    def select_next_subtask(self, task: Task) -> Subtask | None:
        """Public wrapper for ``_select_next_subtask``.

        Returns the next subtask to assign, respecting priority and
        dependencies.  This method is used by external callers (e.g.
        ``local_worker``) that need to iterate subtasks without
        accessing the private implementation.

        Args:
            task: The task whose subtasks to consider.

        Returns:
            The next Subtask to assign, or None if no subtask is ready.
        """
        return self._select_next_subtask(task)

    def _select_worker(self, subtask: Subtask) -> str | None:
        """Select the best available worker for a subtask.

        Strategy: pick the worker with the lowest current load that
        has relevant capabilities.

        Args:
            subtask: The subtask to assign.

        Returns:
            Worker ID, or None if no suitable worker found.
        """
        candidates = [w for w in self.workers.values() if w.is_available]

        if not candidates:
            return None

        # Sort by current load (ascending) then by max capacity (descending)
        candidates.sort(key=lambda w: (w.current_load, -w.max_capacity))
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

    def _parse_decomposition(
        self,
        response: LLMResponse,
        parent_task_id: str,
    ) -> list[Subtask]:
        """Parse LLM decomposition response into Subtask objects.

        Args:
            response: The LLM response.
            parent_task_id: The parent task ID.

        Returns:
            List of Subtask objects.
        """
        text = response.text.strip()

        # Try to extract JSON from the response
        # The LLM may wrap JSON in markdown code blocks
        if "```" in text:
            lines = text.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```"):
                    in_block = not in_block
                    continue
                if in_block:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse decomposition JSON: %s", e)
            logger.debug("Raw LLM response: %s", response.text)
            raise RuntimeError(f"Failed to parse LLM decomposition output: {e}") from e

        if not isinstance(items, list):
            raise RuntimeError(f"Expected JSON array from decomposition, got {type(items)}")

        return self._parse_decomposition_items(items, parent_task_id)

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
        """Gather relevant context from memory for task decomposition."""
        if self.engine is None:
            return "(no engine available)"

        try:
            # Search for relevant project knowledge
            results = self.engine.search_memory(
                query=task.description,
                scope_type="project" if task.project_id else "all",
                project_id=task.project_id,
                max_results=5,
                min_score=0.3,
            )
            if results:
                lines = []
                for r in results:
                    content = getattr(r, "content", None) or getattr(r, "entry", None)
                    if content:
                        text = getattr(content, "text", None) or str(content)
                        lines.append(f"- {text[:200]}")
                return "\n".join(lines)
        except Exception:
            logger.debug("Memory search failed", exc_info=True)

        return "(no relevant memory found)"

    async def _gather_code_context(self, task: Task) -> str:
        """Gather relevant code context via search for task decomposition."""
        if self.engine is None:
            return "(no engine available)"

        try:
            from ultimate_coders.search import SearchQuery

            query = SearchQuery(task.description).limit(5)
            result = self.engine.search(query)
            if result and hasattr(result, "items") and result.items:
                lines = []
                for item in result.items[:5]:
                    snippet = getattr(item, "content_snippet", "")
                    path = getattr(item, "file_path", "unknown")
                    lines.append(f"- {path}: {snippet[:150]}")
                return "\n".join(lines)
        except Exception:
            logger.debug("Code search failed", exc_info=True)

        return "(no relevant code found)"

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

        Uses the engine's checkpoint system to persist the task state
        for recovery.

        Args:
            task_id: The task ID to checkpoint.

        Returns:
            The snapshot ID, or None if checkpointing failed.
        """
        if self.engine is None:
            logger.warning("No engine available for checkpoint")
            return None

        try:
            snapshot_id = self.engine.checkpoint_task(task_id)
            logger.info("Created checkpoint for task %s: %s", task_id, snapshot_id)
            return snapshot_id
        except Exception:
            logger.warning("Failed to checkpoint task %s", task_id, exc_info=True)
            return None

    async def recover_task(self, task_id: str) -> dict | None:
        """Recover a task from the latest checkpoint.

        Args:
            task_id: The task ID to recover.

        Returns:
            The recovered task state dict, or None if recovery failed.
        """
        if self.engine is None:
            logger.warning("No engine available for recovery")
            return None

        try:
            state = self.engine.recover_task(task_id)
            logger.info("Recovered task %s", task_id)
            return state
        except Exception:
            logger.warning("Failed to recover task %s", task_id, exc_info=True)
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
        resolver = ConflictResolver(llm_client=self.llm_client)
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

    def acquire_rate_limit(self, estimated_tokens: float = 1000.0) -> bool:
        """Try to acquire LLM rate limit capacity.

        Args:
            estimated_tokens: Estimated token consumption.

        Returns:
            True if capacity is available.
        """
        return self.rate_limiter.try_acquire(estimated_tokens)

    def release_rate_limit(self) -> None:
        """Release rate limit capacity after an LLM request completes."""
        self.rate_limiter.release()

    def check_circuit_breaker(self) -> bool:
        """Check if the circuit breaker allows a request.

        Returns:
            True if the request can proceed.
        """
        return self.circuit_breaker.allow_request()

    def record_llm_success(self) -> None:
        """Record a successful LLM API call."""
        self.circuit_breaker.record_success()

    def record_llm_failure(self) -> None:
        """Record a failed LLM API call."""
        self.circuit_breaker.record_failure()

    def reset_circuit_breaker(self) -> bool:
        """Reset the circuit breaker to closed state.

        Returns:
            True if the circuit breaker was reset, False if not available.
        """
        if self.circuit_breaker is not None:
            self.circuit_breaker.reset()
            return True
        return False

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
        logger.info("Task %s resumed", task_id)
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
