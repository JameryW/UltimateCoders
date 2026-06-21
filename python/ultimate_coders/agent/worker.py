"""Worker — executes subtasks via sandbox agent (Claude Code / Codex).

Previously supported an LLM tool-calling mode — removed in favor of
sandbox-only execution. Coding agents (Claude Code, Codex) have their
own tool chains and don't need a Python-side tool-calling loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictResult,
    EditIntent,
    EditType,
    LineRange,
)
from ultimate_coders.agent.sandbox import (
    AgentOutput,
    SandboxConfig,
    SandboxManager,
)
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    WorkerInfo,
)

logger = logging.getLogger(__name__)

_SUBTASK_USER_TEMPLATE = """\
Subtask: {description}

Expected output: {expected_output}

File constraints (do NOT modify): {file_constraints}
"""


class Worker:
    """Executes subtasks via sandbox agent.

    Usage:
        worker = Worker(engine=engine, sandbox_config=config)
        result = await worker.execute_subtask(subtask)
    """

    def __init__(
        self,
        worker_id: str = "",
        engine: Any = None,
        capabilities: list[str] | None = None,
        max_capacity: int = 3,
        conflict_detector: ConflictDetector | None = None,
        sandbox_config: SandboxConfig | None = None,
        event_emitter: Any | None = None,
        nats_publisher: Any | None = None,
    ):
        self.worker_id = worker_id or str(uuid.uuid4())
        self.engine = engine
        self.capabilities = capabilities or ["code", "search", "memory", "test"]
        self.max_capacity = max_capacity
        self.current_task: Subtask | None = None
        self._active_count = 0
        self.conflict_detector = conflict_detector or ConflictDetector()

        # Sandbox execution (always)
        self._sandbox_config = sandbox_config or SandboxConfig()
        self._sandbox_manager = SandboxManager(self._sandbox_config, engine)

        # Event publishing — NATS (preferred) or local event_emitter fallback
        self.nats_publisher = nats_publisher
        self.event_emitter = event_emitter

    def get_info(self) -> WorkerInfo:
        return WorkerInfo(
            id=self.worker_id,
            capabilities=self.capabilities,
            current_load=self._active_count,
            max_capacity=self.max_capacity,
        )

    async def _publish_event(
        self,
        event_type: str,
        task_id: str = "",
        subtask_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Publish event via NATS (preferred) or local event_emitter fallback."""
        if self.nats_publisher is not None:
            await self.nats_publisher.publish_event(
                event_type, task_id=task_id, subtask_id=subtask_id, data=data,
            )
        elif self.event_emitter is not None:
            await self.event_emitter.emit(
                event_type, task_id=task_id, subtask_id=subtask_id, data=data,
            )

    async def execute_subtask(self, subtask: Subtask) -> SubtaskResult:
        """Execute a subtask via sandbox agent."""
        self.current_task = subtask
        self._active_count += 1
        subtask.status = SubtaskStatus.IN_PROGRESS

        await self._publish_event(
            "subtask_started",
            task_id=subtask.parent_id,
            subtask_id=subtask.id,
            data={"description": subtask.description, "worker_id": self.worker_id},
        )

        try:
            timeout_secs = subtask.timeout_seconds or 600
            try:
                result = await asyncio.wait_for(
                    self._execute_in_sandbox(subtask),
                    timeout=timeout_secs,
                )
            except asyncio.TimeoutError:
                result = SubtaskResult(
                    subtask_id=subtask.id,
                    worker_id=self.worker_id,
                    summary=f"Subtask timed out after {timeout_secs}s",
                    success=False,
                )

            if result.success:
                await self._publish_event(
                    "subtask_completed",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data={
                        "summary": result.summary[:300],
                        "success": True,
                        "modified_files": [
                            {"path": f.file_path, "type": f.change_type.value}
                            for f in (result.modified_files or [])
                        ],
                    },
                )
            else:
                await self._publish_event(
                    "subtask_failed",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data={"error": result.summary[:300], "worker_id": self.worker_id},
                )
            return result

        except Exception as e:
            logger.error("Subtask %s execution failed: %s", subtask.id, e, exc_info=True)
            await self._publish_event(
                "subtask_failed",
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                data={"error": str(e), "worker_id": self.worker_id},
            )
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary=f"Execution error: {e}",
                success=False,
            )

        finally:
            self.current_task = None
            self._active_count = max(0, self._active_count - 1)

    async def _execute_in_sandbox(self, subtask: Subtask) -> SubtaskResult:
        """Execute via sandbox (Claude Code / Codex)."""
        prompt = _SUBTASK_USER_TEMPLATE.format(
            description=subtask.description,
            expected_output=subtask.expected_output or "Complete the described task",
            file_constraints=", ".join(subtask.file_constraints) or "none",
        )
        output: AgentOutput = await self._sandbox_manager.execute(prompt)
        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id=self.worker_id,
            modified_files=output.file_changes,
            summary=output.summary,
            success=output.success,
        )

    async def send_heartbeat(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "capabilities": self.capabilities,
            "current_load": self._active_count,
            "max_capacity": self.max_capacity,
        }

    def declare_edit_intent(
        self,
        file_path: str,
        edit_type: EditType = EditType.MODIFY,
        regions: list[tuple[int, int]] | None = None,
    ) -> tuple[ConflictResult, dict | None]:
        line_ranges = [LineRange(start=s, end=e) for s, e in (regions or [])]
        intent = EditIntent(
            worker_id=self.worker_id,
            file_path=file_path,
            edit_type=edit_type,
            regions=line_ranges,
        )
        result, info = self.conflict_detector.declare_intent(intent)
        if result != ConflictResult.NO_CONFLICT:
            logger.warning(
                "Conflict detected for %s: %s (workers: %s)",
                file_path, result.value,
                info.conflicting_workers if info else [],
            )
        return result, info

    def release_edit_intent(self, file_path: str) -> None:
        self.conflict_detector.remove_intent(file_path, self.worker_id)
