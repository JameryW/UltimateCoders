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
from ultimate_coders.agent.llm import LLMResponse
from ultimate_coders.agent.types import (
    AgentEvent,
    AgentEventType,
    AgentRunConfig,
    ExecutionSpec,
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
- "files": List of file paths that this subtask WILL modify (used for conflict detection)
- "expected_output": What the completed subtask should produce

Guidelines:
- Keep subtasks small and focused (one logical change per subtask)
- Max {max_subtasks} subtasks
- Order subtasks so earlier ones provide context for later ones
- Make dependencies minimal but correct (don't create unnecessary serialization)
- Include a "research" subtask first if the task requires understanding existing code
- Include a "test" subtask if the task involves writing code
- List files each subtask will modify in "files" — this prevents parallel conflicts

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
        llm_client: Any | None = None,
        codegraph_client: Any | None = None,
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
            llm_client: Optional LLMClient for planning and Q&A.
                When not provided, planning/Q&A features are disabled
                but task decomposition still works via sandbox.
            codegraph_client: Optional CodegraphClient for AST-aware
                code search. When not provided, codegraph tools are
                unavailable in the planning loop.
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
        # LLM + Codegraph for planning and Q&A
        self.llm_client = llm_client
        self.codegraph_client = codegraph_client
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
            # Auto-schedule ready subtasks (no dependencies or all deps met)
            await self.schedule_ready_subtasks(task)
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

        When an LLM client is configured, first gathers code context
        via ``plan_task()`` and injects it into the decomposition prompt.
        The sandbox agent (Claude Code) still performs the actual
        decomposition, but with richer context about the codebase.

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

        # Gather planning context (LLM agent loop or direct search)
        planning_spec = None
        if self.llm_client is not None:
            try:
                planning_spec = await self.plan_task(task)
            except Exception:
                logger.warning(
                    "plan_task() failed for task %s, proceeding without context",
                    task.id[:8], exc_info=True,
                )

        # Build context string from ExecutionSpec or fallback
        planning_context = ""
        if planning_spec is not None and planning_spec.raw_text:
            # Use structured spec + raw_text
            parts: list[str] = []
            if planning_spec.context:
                parts.append(f"## Codebase Context\n{planning_spec.context}")
            if planning_spec.approach:
                steps = "\n".join(
                    f"{i+1}. {s}" for i, s in enumerate(planning_spec.approach)
                )
                parts.append(f"## Suggested Approach\n{steps}")
            if planning_spec.critical_files:
                files = "\n".join(f"- {f}" for f in planning_spec.critical_files)
                parts.append(f"## Critical Files\n{files}")
            if planning_spec.verification:
                parts.append(f"## Verification\n{planning_spec.verification}")
            planning_context = "\n\n".join(parts)
        elif planning_spec is None:
            # Fallback: direct context gathering without LLM
            memory_ctx = await self._gather_memory_context(task)
            code_ctx = await self._gather_code_context(task)
            raw_parts = [p for p in [memory_ctx, code_ctx] if p]
            planning_context = "\n\n".join(raw_parts)

        # Build the decomposition prompt with context
        system = _DECOMPOSE_SYSTEM_PROMPT.format(
            max_subtasks=self.config.max_subtasks,
        )
        user_msg = _DECOMPOSE_USER_TEMPLATE.format(
            project_id=task.project_id or "unknown",
            description=task.description,
        )

        # Inject planning context if available
        if planning_context:
            user_msg += f"\n\n## Codebase Context\n{planning_context}"

        # Combine system + user into a single prompt for `claude -p`
        combined_prompt = f"{system}\n\n{user_msg}"
        logger.info(
            "Decomposing task %s via sandbox (prompt_len=%d, has_context=%s)",
            task.id, len(combined_prompt), bool(planning_context),
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
        subtasks = self._parse_decomposition_items(items, task.id)
        # Validate decomposition quality; retry once if invalid
        if not self._validate_decomposition(subtasks, task):
            logger.warning(
                "Decomposition validation failed for task %s, retrying once",
                task.id,
            )
            result2 = await self.sandbox_manager.execute_decompose(request)
            output2 = adapter.parse_output(result2)
            if output2.success:
                items2 = parse_decomposition_output(result2.stdout)
                subtasks2 = self._parse_decomposition_items(items2, task.id)
                if self._validate_decomposition(subtasks2, task):
                    return subtasks2
                logger.warning("Retry decomposition still invalid for task %s", task.id)
        return subtasks

    def _validate_decomposition(
        self, subtasks: list[Subtask], task: Task,
    ) -> bool:
        """Validate decomposition quality.

        Returns False if the decomposition is clearly broken:
        - Zero subtasks
        - All subtasks have empty descriptions
        - Single subtask that just restates the parent task
        - More than max_subtasks
        """
        if not subtasks:
            return False
        if len(subtasks) > self.config.max_subtasks:
            return False
        # All descriptions empty
        if all(not st.description.strip() for st in subtasks):
            return False
        # ponytail: single subtask rehashing parent — likely failed decomposition
        if len(subtasks) == 1:
            desc = subtasks[0].description.strip().lower()
            parent = task.description.strip().lower()
            if desc == parent or desc in parent:
                return False
        return True

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

    async def schedule_ready_subtasks(self, task: Task) -> list[str]:
        """Assign all ready (pending, dependencies met) subtasks to workers.

        Returns list of assigned worker IDs. Called automatically after
        task decomposition and after subtask completion (when new
        subtasks may become unblocked).

        ponytail: simple greedy — no priority queue, upgrade if needed.
        """
        assigned: list[str] = []
        # Build a set of completed subtask IDs for dependency checking
        completed_ids = {st.id for st in task.subtasks if st.is_complete}
        for subtask in task.subtasks:
            if subtask.status != SubtaskStatus.PENDING:
                continue
            # Check dependencies — all must be completed
            deps_met = all(dep_id in completed_ids for dep_id in subtask.depends_on)
            if not deps_met:
                continue
            wid = await self.assign_subtask(subtask)
            if wid is not None:
                assigned.append(wid)
        return assigned

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
                        "retry_count": subtask.retry_count,
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
                    "result": result.summary[:50000],
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

        # Filter out subtasks with file conflicts (active intents from other workers)
        conflict_free = []
        for st in candidates:
            if not st.file_constraints:
                conflict_free.append(st)
                continue
            # Check each file in constraints against active intents
            blocked = False
            for fp in st.file_constraints:
                intents = self.conflict_detector.get_intents(fp)
                # Blocked if another worker has an active intent on this file
                if any(i.worker_id != st.assigned_worker for i in intents):
                    blocked = True
                    break
            if not blocked:
                conflict_free.append(st)
            else:
                logger.debug(
                    "Subtask %s blocked by file conflict: %s",
                    st.id[:8], st.file_constraints,
                )

        if not conflict_free:
            # All candidates blocked — fall back to first candidate
            # (deadlock prevention: if everything is blocked, pick one anyway)
            candidates.sort(key=lambda st: -st.priority)
            return candidates[0]

        # Sort by priority descending (higher priority first)
        conflict_free.sort(key=lambda st: -st.priority)
        return conflict_free[0]

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
                file_constraints=item.get("file_constraints", [])
                    or item.get("files", []),
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
        """Gather memory context for task decomposition.

        Uses engine.read_memory to find relevant stored context.
        Returns truncated markdown summary, or empty string.
        """
        if self.engine is None:
            return ""
        try:
            raw = self.engine.read_memory(
                key_scope="task",
                key="task_definition",
            )
            if raw:
                return self._truncate(f"Task memory: {raw}")
        except Exception:
            logger.debug("Failed to read task memory for context", exc_info=True)
        return ""

    async def _gather_code_context(self, task: Task) -> str:
        """Gather code context for task decomposition.

        Uses engine.search_code and codegraph to find relevant code.
        Returns truncated markdown summary, or empty string.
        """
        parts: list[str] = []

        # Engine search
        if self.engine is not None and hasattr(self.engine, "search_code"):
            try:
                results = self.engine.search_code(
                    query=task.description,
                    modes=["hybrid"],
                    max_results=5,
                )
                if results:
                    parts.append("## Search Results")
                    for r in results[:5]:
                        snippet = getattr(r, "content_snippet", str(r))[:200]
                        path = getattr(r, "file_path", "")
                        parts.append(f"- {path}: {snippet}")
            except Exception:
                logger.debug("Code search for context failed", exc_info=True)

        # Codegraph explore
        if self.codegraph_client is not None and self.codegraph_client.is_available():
            try:
                explore_result = self.codegraph_client.explore(
                    task.description, max_nodes=8,
                )
                if explore_result:
                    parts.append(explore_result)
            except Exception:
                logger.debug("Codegraph explore for context failed", exc_info=True)

        if not parts:
            return ""
        combined = "\n\n".join(parts)
        return self._truncate(combined)

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

    # ── Agent Capabilities: Planning & Q&A ──────────────────────

    # Orchestration rules (inspired by oh-my-pi's orchestrate-notice)
    _ORCHESTRATE_NOTICE = """\
ORCHESTRATE RULES:
1. Decompose: Break into independently executable subtasks
2. Dispatch: Each assignment must be self-contained (no assumptions)
3. Verify: Check outputs after each phase
4. Iterate: Respawn incomplete work rather than patching inline
5. Enumerate: List full surface area before dispatching
6. Parallelize: Maximize parallel execution; minimize serialization
"""

    # Tools that mutate state and must run exclusively (not concurrently)
    _EXCLUSIVE_TOOLS: frozenset[str] = frozenset()

    def _build_tools(self) -> list:
        """Build tool definitions for the Orchestrator's agent loop.

        Returns list of ToolDefinition objects. Tools that require
        unavailable backends (engine, codegraph) are omitted.
        """
        from ultimate_coders.agent.llm import make_tool_definition

        tools: list = []

        if self.engine is not None and hasattr(self.engine, "search_code"):
            tools.append(make_tool_definition(
                "search_code",
                "Search the codebase for relevant code. "
                "Returns file paths and content snippets.",
                {
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                        "required": True,
                    },
                    "modes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Search modes: text, semantic, ast, hybrid",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 5)",
                    },
                },
            ))

        if self.engine is not None:
            tools.append(make_tool_definition(
                "search_memory",
                "Search stored memory for relevant context "
                "about past tasks, decisions, and results.",
                {
                    "key_scope": {
                        "type": "string",
                        "description": "Memory scope (task, checkpoint, etc.)",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key prefix to search",
                        "required": True,
                    },
                },
            ))

        if self.codegraph_client is not None and self.codegraph_client.is_available():
            tools.append(make_tool_definition(
                "codegraph_explore",
                "Explore code structure via AST-aware search. "
                "Returns symbols, dependencies, and impact analysis.",
                {
                    "query": {
                        "type": "string",
                        "description": "Natural language query or symbol name",
                        "required": True,
                    },
                },
            ))

        if self.engine is not None:
            tools.append(make_tool_definition(
                "read_file",
                "Read a file's content from the project. "
                "Returns up to 200 lines.",
                {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to read",
                        "required": True,
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line (1-based, default 1)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "End line (default start+200)",
                    },
                },
            ))

        return tools

    async def _execute_tool(self, tool_call: Any) -> str:
        """Execute a tool call and return the result string.

        Args:
            tool_call: ToolCall object with name, input, id.

        Returns:
            Tool result string, truncated to config.tool_result_max_chars.
        """
        name = tool_call.name
        inp = tool_call.input if isinstance(tool_call.input, dict) else {}
        max_chars = self.config.tool_result_max_chars

        try:
            if name == "search_code" and self.engine is not None:
                query = inp.get("query", "")
                modes = inp.get("modes", ["hybrid"])
                max_results = inp.get("max_results", 5)
                results = self.engine.search_code(
                    query=query, modes=modes, max_results=max_results,
                )
                parts: list[str] = []
                for r in results:
                    path = getattr(r, "file_path", "")
                    snippet = getattr(r, "content_snippet", str(r))[:200]
                    score = getattr(r, "score", 0)
                    parts.append(f"{path} (score={score:.2f}): {snippet}")
                return self._truncate("\n".join(parts), max_chars)

            if name == "search_memory" and self.engine is not None:
                key_scope = inp.get("key_scope", "")
                key = inp.get("key", "")
                raw = self.engine.read_memory(key_scope=key_scope, key=key)
                if raw is not None:
                    return self._truncate(str(raw), max_chars)
                return "(no memory found)"

            if name == "codegraph_explore" and self.codegraph_client is not None:
                query = inp.get("query", "")
                result = self.codegraph_client.explore(query, max_nodes=10)
                return self._truncate(result, max_chars) if result else "(no results)"

            if name == "read_file" and self.engine is not None:
                file_path = inp.get("file_path", "")
                start = inp.get("start_line", 1)
                end = inp.get("end_line", start + 200)
                if hasattr(self.engine, "read_file"):
                    content = self.engine.read_file(
                        file_path, start_line=start, end_line=end,
                    )
                    return self._truncate(str(content), max_chars)
                return "(read_file not supported by engine)"

            return f"Unknown tool: {name}"

        except Exception as e:
            logger.warning("Tool %s execution failed: %s", name, e)
            return f"Error: {e}"

    def _truncate(self, text: str, max_chars: int | None = None) -> str:
        """Truncate text to max_chars with ellipsis."""
        limit = max_chars or self.config.tool_result_max_chars
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """Rough token count: ~4 chars per token for English/code.

        ponytail: crude estimate, 80% threshold gives safety margin.
        Upgrade to tiktoken if accuracy matters.
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("content", block.get("text", ""))
                        total += len(str(text)) // 4
        return total

    async def _compact_context(
        self,
        working_messages: list[dict],
        keep_recent: int = 4,
    ) -> list[dict]:
        """Summarize old tool results when approaching token budget.

        Keeps the last `keep_recent` messages intact. Summarizes
        everything before into a single assistant message. Falls
        back to truncation if no LLM is available for summarization.
        """
        if len(working_messages) <= keep_recent + 1:
            return working_messages

        old = working_messages[:-keep_recent]
        recent = working_messages[-keep_recent:]

        # Build summary of old messages
        summary_parts: list[str] = []
        for msg in old:
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                summary_parts.append(content[:500])
            elif isinstance(content, list):
                for block in content:
                    text = block.get("content", block.get("text", ""))
                    if isinstance(text, str) and text.strip():
                        summary_parts.append(text[:500])

        summary_text = "\n".join(summary_parts)
        if not summary_text.strip():
            return recent

        if self.llm_client is None:
            # No LLM — just keep recent messages
            return recent

        try:
            summary_response = await self.llm_client.complete(
                messages=[{"role": "user", "content": (
                    "Summarize the following tool results and context into "
                    "a concise summary preserving key file paths, function "
                    "names, and findings. Omit verbose output.\n\n"
                    + summary_text
                )}],
                system="You are a context compaction assistant. "
                       "Produce concise summaries preserving key facts.",
                max_tokens=1024,
            )
            compacted = [
                {
                    "role": "assistant",
                    "content": f"[Context Summary]\n{summary_response.text}",
                },
            ] + recent
            logger.info("Compacted %d old messages into summary", len(old))
            return compacted
        except Exception:
            logger.warning("Context compaction failed, keeping recent only")
            return recent

    async def _agent_loop(
        self,
        messages: list[dict],
        tools: list | None = None,
        system: str | None = None,
        run_config: AgentRunConfig | None = None,
    ) -> tuple[LLMResponse, list[dict[str, Any]], list[AgentEvent]]:
        """Turn-based agent loop with abort, steering, and events.

        Inspired by oh-my-pi's agent-loop.ts: a turn-based loop that
        streams LLM responses, executes tool calls with concurrency
        control, compacts context when approaching budget, and emits
        granular lifecycle events.

        Args:
            messages: Initial conversation messages.
            tools: Available tool definitions (None = no tool calling).
            system: System prompt.
            run_config: Loop configuration (max_turns, budget, abort, steering).

        Returns:
            Tuple of (final LLMResponse, tool_calls_log, AgentEvent list).
        """
        import asyncio

        cfg = run_config or AgentRunConfig(
            max_turns=self.config.planning_max_tool_rounds,
            token_budget=self.config.planning_context_budget,
        )
        working_messages = list(messages)
        events: list[AgentEvent] = []
        tool_log: list[dict[str, Any]] = []
        final_response = LLMResponse()  # ponytail: default empty response

        events.append(AgentEvent(type=AgentEventType.AGENT_START))

        for turn in range(cfg.max_turns):
            # ── Check abort ──
            if cfg.abort_event is not None and cfg.abort_event.is_set():
                logger.info("Agent loop aborted at turn %d", turn)
                events.append(AgentEvent(
                    type=AgentEventType.AGENT_END, turn=turn,
                    data={"reason": "abort"},
                ))
                return final_response, tool_log, events

            # ── Drain steering messages ──
            if cfg.steering_queue is not None:
                while not cfg.steering_queue.empty():
                    try:
                        msg = cfg.steering_queue.get_nowait()
                        working_messages.append(msg)
                    except asyncio.QueueEmpty:
                        break

            events.append(AgentEvent(type=AgentEventType.TURN_START, turn=turn))

            # ── LLM call ──
            try:
                if tools:
                    response = await self.llm_client.complete_with_tools(
                        messages=working_messages,
                        tools=tools,
                        system=system,
                        max_tokens=2048,
                        max_tool_rounds=1,  # ponytail: 1 round per turn; we control the loop
                        tool_executor=self._execute_tool,
                    )
                    # complete_with_tools with max_tool_rounds=1 returns
                    # after one LLM call + tool execution
                    llm_response = response[0]
                    tool_results = response[1]
                    tool_log.extend(tool_results)

                    # Emit tool events
                    for entry in tool_results:
                        tc = entry.get("tool_call", {})
                        events.append(AgentEvent(
                            type=AgentEventType.TOOL_START, turn=turn,
                            data={"tool": tc.get("name"), "id": tc.get("id")},
                        ))
                        events.append(AgentEvent(
                            type=AgentEventType.TOOL_END, turn=turn,
                            data={
                                "tool": tc.get("name"),
                                "result_len": len(entry.get("result", "")),
                            },
                        ))

                    # Update working_messages from the tool-calling results
                    # (complete_with_tools modifies its internal working_messages,
                    # but we need to track our own)
                    # Re-send with accumulated context
                    if llm_response.has_tool_calls:
                        # Tool calls were executed — need to continue the loop
                        # Reconstruct messages for next turn
                        # The LLM response + tool results are already in working_messages
                        # via the complete_with_tools internal loop
                        # We need to sync: add the final response text if present
                        final_response = llm_response
                        working_messages.append({
                            "role": "assistant",
                            "content": llm_response.text or "...",
                        })
                    else:
                        final_response = llm_response
                        events.append(AgentEvent(
                            type=AgentEventType.TURN_END, turn=turn,
                        ))
                        break

                else:
                    # No tools — direct completion
                    llm_response = await self.llm_client.complete(
                        messages=working_messages,
                        system=system,
                        max_tokens=4096,
                    )
                    final_response = llm_response
                    events.append(AgentEvent(
                        type=AgentEventType.TURN_END, turn=turn,
                    ))
                    break

            except asyncio.CancelledError:
                logger.info("Agent loop cancelled at turn %d", turn)
                events.append(AgentEvent(
                    type=AgentEventType.AGENT_ERROR, turn=turn,
                    data={"error": "cancelled"},
                ))
                events.append(AgentEvent(
                    type=AgentEventType.AGENT_END, turn=turn,
                    data={"reason": "cancelled"},
                ))
                return final_response, tool_log, events

            except Exception as e:
                logger.error("Agent loop error at turn %d: %s", turn, e)
                events.append(AgentEvent(
                    type=AgentEventType.AGENT_ERROR, turn=turn,
                    data={"error": str(e)},
                ))
                events.append(AgentEvent(
                    type=AgentEventType.AGENT_END, turn=turn,
                    data={"reason": "error"},
                ))
                return final_response, tool_log, events

            # ── Check token budget → compact if needed ──
            estimated = self._estimate_tokens(working_messages)
            if estimated > cfg.token_budget * 0.8:
                working_messages = await self._compact_context(working_messages)
                logger.info(
                    "Context compacted at turn %d: estimated %d tokens",
                    turn, estimated,
                )

            events.append(AgentEvent(type=AgentEventType.TURN_END, turn=turn))

        events.append(AgentEvent(
            type=AgentEventType.AGENT_END,
            data={"turns": turn + 1, "tool_calls": len(tool_log)},
        ))
        return final_response, tool_log, events

    async def plan_task(self, task: Task) -> ExecutionSpec:
        """Plan a task via the agent loop, producing an ExecutionSpec.

        Uses the LLM to autonomously search the codebase, explore
        code structure, and read relevant files. Returns a structured
        execution spec with context, approach, critical files,
        verification, and assumptions.

        Args:
            task: The task to plan.

        Returns:
            ExecutionSpec with gathered code context and plan.
            raw_text is populated for sandbox decomposition fallback.
            Returns ExecutionSpec with empty raw_text if LLM is not
            configured.
        """
        if self.llm_client is None:
            # Fallback: gather context without LLM
            memory_ctx = await self._gather_memory_context(task)
            code_ctx = await self._gather_code_context(task)
            parts = [p for p in [memory_ctx, code_ctx] if p]
            raw = "\n\n".join(parts) if parts else ""
            return ExecutionSpec(context=raw, raw_text=raw)

        tools = self._build_tools()
        if not tools:
            memory_ctx = await self._gather_memory_context(task)
            code_ctx = await self._gather_code_context(task)
            parts = [p for p in [memory_ctx, code_ctx] if p]
            raw = "\n\n".join(parts) if parts else ""
            return ExecutionSpec(context=raw, raw_text=raw)

        system = self._ORCHESTRATE_NOTICE + (
            "\nYou are a task planning assistant. Given a task, produce "
            "an EXECUTION SPEC (not a design document). Every choice "
            "must be pre-made so an implementer can execute top-to-bottom "
            "with ZERO design decisions.\n\n"
            "Output structure:\n"
            "## Context\n- What we know about the codebase\n\n"
            "## Approach (ordered steps)\n"
            "1. Step — file:line anchor, what to change\n\n"
            "## Critical Files & Anchors\n"
            "- path/to/file.py:ClassName.method_name\n\n"
            "## Verification\n"
            "- How to confirm each step worked\n\n"
            "## Assumptions & Contingencies\n"
            "- If X is not as assumed, do Y\n\n"
            "Use the available tools to gather context. "
            "Be efficient — 2-3 tool calls are usually enough. "
            "Do NOT write code or make changes."
        )

        messages = [
            {
                "role": "user",
                "content": (
                    f"Project: {task.project_id or 'unknown'}\n"
                    f"Task: {task.description}"
                ),
            },
        ]

        run_config = AgentRunConfig(
            max_turns=self.config.planning_max_tool_rounds,
            token_budget=self.config.planning_context_budget,
        )

        response, tool_log, _events = await self._agent_loop(
            messages=messages,
            tools=tools,
            system=system,
            run_config=run_config,
        )

        # Parse LLM response into ExecutionSpec
        spec = self._parse_execution_spec(response.text or "")
        # Fallback: include tool results in raw_text for sandbox path
        tool_parts: list[str] = []
        for entry in tool_log:
            tc = entry.get("tool_call", {})
            tool_parts.append(
                f"### Tool: {tc.get('name', '?')}\n{entry.get('result', '')}"
            )
        if spec.raw_text:
            tool_parts.append(f"### Plan\n{spec.raw_text}")
        spec.raw_text = "\n\n".join(tool_parts) if tool_parts else spec.raw_text

        logger.info(
            "Planned task %s: %d tool calls, spec has %d approach steps",
            task.id[:8], len(tool_log), len(spec.approach),
        )
        return spec

    def _parse_execution_spec(self, text: str) -> ExecutionSpec:
        """Parse LLM text output into an ExecutionSpec.

        Tries to extract sections by ## headers. Falls back to
        raw_text-only if parsing fails.
        """
        sections: dict[str, str] = {}
        current_section = ""
        current_lines: list[str] = []

        for line in text.splitlines():
            if line.startswith("## ") or line.startswith("# "):
                if current_section and current_lines:
                    sections[current_section] = "\n".join(current_lines).strip()
                current_section = line.lstrip("# ").strip()
                current_lines = []
            else:
                current_lines.append(line)

        if current_section and current_lines:
            sections[current_section] = "\n".join(current_lines).strip()

        # Map sections to ExecutionSpec fields
        context = sections.get("Context", "")
        approach_text = sections.get("Approach", "")
        # Parse numbered steps into list
        approach: list[str] = []
        for line in approach_text.splitlines():
            stripped = line.strip()
            if stripped and (stripped[0].isdigit() or stripped.startswith("-")):
                approach.append(stripped.lstrip("0123456789.- ").strip())
        if not approach and approach_text.strip():
            approach = [approach_text.strip()]

        critical_files: list[str] = []
        cf_text = sections.get("Critical Files & Anchors", "")
        for line in cf_text.splitlines():
            stripped = line.strip()
            if stripped and stripped.startswith("-"):
                critical_files.append(stripped.lstrip("- ").strip())

        verification = sections.get("Verification", "")
        assumptions = sections.get("Assumptions & Contingencies", "")

        return ExecutionSpec(
            context=context,
            approach=approach,
            critical_files=critical_files,
            verification=verification,
            assumptions=assumptions,
            raw_text=text,
        )

    async def ask(
        self,
        question: str,
        project_id: str = "",
    ) -> str:
        """Answer a question about the codebase via the agent loop.

        Uses the LLM to search the codebase and produce an answer.
        Falls back to a direct LLM completion if no tools are available.

        Args:
            question: The question to answer.
            project_id: Optional project context.

        Returns:
            Answer string. Empty if LLM client is not configured.
        """

        if self.llm_client is None:
            return "(LLM client not configured — cannot answer questions)"

        tools = self._build_tools()

        system = (
            "You are a codebase expert assistant. "
            "Answer questions about the codebase using the available "
            "search and exploration tools. Be concise and specific — "
            "cite file paths and line numbers. "
            "If you cannot find the answer, say so."
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Project: {project_id or 'unknown'}\n"
                    f"Question: {question}"
                ),
            },
        ]

        run_config = AgentRunConfig(
            max_turns=self.config.planning_max_tool_rounds,
            token_budget=self.config.planning_context_budget,
        )

        response, _tool_log, _events = await self._agent_loop(
            messages=messages,
            tools=tools if tools else None,
            system=system,
            run_config=run_config,
        )
        return response.text

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
                # Elastic scaling signal: pending subtasks exist but no workers available
                pending = len(ready)
                if pending > 0 and len(self.workers) == 0:
                    await self._publish_event(
                        "worker_scale_up",
                        task_id=task.id,
                        data={
                            "reason": "no_workers_available",
                            "pending_subtasks": pending,
                            "active_workers": len(self.workers),
                            "desired_workers": min(pending, self.config.max_subtasks),
                        },
                    )
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
