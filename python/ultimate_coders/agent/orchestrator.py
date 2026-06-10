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
from typing import Any, Callable, Dict, List, Optional

from ultimate_coders.agent.llm import LLMClient, LLMResponse, make_tool_definition
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
        llm_client: Optional[LLMClient] = None,
        config: Optional[OrchestratorConfig] = None,
    ):
        """Initialize the Orchestrator.

        Args:
            engine: Engine instance for memory/search operations.
            llm_client: LLM client for task decomposition.
            config: Orchestrator configuration.
        """
        self.engine = engine
        self.llm_client = llm_client
        self.config = config or OrchestratorConfig()
        self.workers: Dict[str, WorkerInfo] = {}
        self.tasks: Dict[str, Task] = {}

    async def submit_task(
        self,
        description: str,
        project_id: Optional[str] = None,
    ) -> Task:
        """Submit a new task for orchestration.

        Creates the task, decomposes it into subtasks via LLM,
        and schedules ready subtasks.

        Args:
            description: The task description.
            project_id: Optional project/repository context.

        Returns:
            The created Task with subtasks.
        """
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

        task.update_timestamp()
        return task

    async def decompose_task(self, task: Task) -> List[Subtask]:
        """Use LLM to decompose a task into subtasks.

        Args:
            task: The task to decompose.

        Returns:
            List of Subtask objects with dependencies.

        Raises:
            RuntimeError: If LLM decomposition fails.
        """
        if self.llm_client is None:
            raise RuntimeError("LLM client is required for task decomposition")

        # Gather context from memory and search
        memory_context = await self._gather_memory_context(task)
        code_context = await self._gather_code_context(task)

        # Build prompt
        system = _DECOMPOSE_SYSTEM_PROMPT.format(
            max_subtasks=self.config.max_subtasks,
        )
        user_msg = _DECOMPOSE_USER_TEMPLATE.format(
            project_id=task.project_id or "unknown",
            description=task.description,
            memory_context=memory_context,
            code_context=code_context,
        )

        # Call LLM
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
        worker_id: Optional[str] = None,
    ) -> Optional[str]:
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
                    content=json.dumps({
                        "subtask_id": subtask.id,
                        "worker_id": worker_id,
                        "assigned_at": datetime.now(timezone.utc).isoformat(),
                    }),
                    content_type="structured",
                    source_agent="orchestrator",
                    task_id=subtask.parent_id,
                    project_id=None,
                )
            except Exception:
                logger.warning("Failed to write assignment to memory", exc_info=True)

        logger.info(
            "Assigned subtask %s to worker %s", subtask.id, worker_id,
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
        elif task.has_failed:
            # Check if all subtasks are either completed or failed
            all_done = all(
                st.is_complete or st.is_failed
                for st in task.subtasks
            )
            if all_done:
                task.status = TaskStatus.FAILED
                task.result = self._aggregate_results(task)
                logger.warning("Task %s failed", task.id)

    async def register_worker(self, worker_info: WorkerInfo) -> None:
        """Register a new worker.

        Args:
            worker_info: Information about the worker.
        """
        self.workers[worker_info.id] = worker_info
        logger.info(
            "Registered worker %s with capabilities: %s",
            worker_info.id, worker_info.capabilities,
        )

    async def unregister_worker(self, worker_id: str) -> None:
        """Unregister a worker.

        Args:
            worker_id: ID of the worker to unregister.
        """
        if worker_id in self.workers:
            del self.workers[worker_id]
            logger.info("Unregistered worker %s", worker_id)

    async def get_task_status(self, task_id: str) -> Optional[Task]:
        """Get current task status.

        Args:
            task_id: The task ID.

        Returns:
            The Task object, or None if not found.
        """
        return self.tasks.get(task_id)

    def get_available_workers(self) -> List[WorkerInfo]:
        """Get all currently available workers."""
        return [w for w in self.workers.values() if w.is_available]

    # ── Private helpers ─────────────────────────────────────────

    def _select_worker(self, subtask: Subtask) -> Optional[str]:
        """Select the best available worker for a subtask.

        Strategy: pick the worker with the lowest current load that
        has relevant capabilities.

        Args:
            subtask: The subtask to assign.

        Returns:
            Worker ID, or None if no suitable worker found.
        """
        candidates = [
            w for w in self.workers.values()
            if w.is_available
        ]

        if not candidates:
            return None

        # Sort by current load (ascending) then by max capacity (descending)
        candidates.sort(key=lambda w: (w.current_load, -w.max_capacity))
        return candidates[0].id

    def _parse_decomposition(
        self,
        response: LLMResponse,
        parent_task_id: str,
    ) -> List[Subtask]:
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
            raise RuntimeError(
                f"Failed to parse LLM decomposition output: {e}"
            ) from e

        if not isinstance(items, list):
            raise RuntimeError(
                f"Expected JSON array from decomposition, got {type(items)}"
            )

        # First pass: create subtasks with temporary index-based deps
        subtask_map: Dict[int, Subtask] = {}
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            subtask = Subtask(
                parent_id=parent_task_id,
                description=item.get("description", f"Subtask {idx + 1}"),
                status=SubtaskStatus.PENDING,
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
                subtask_map[i].id
                for i in dep_indices
                if i in subtask_map
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
