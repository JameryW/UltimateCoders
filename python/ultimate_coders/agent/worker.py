"""Worker — executes subtasks via sandbox agent (Claude Code / Codex).

Previously supported an LLM tool-calling mode — removed in favor of
sandbox-only execution. Coding agents (Claude Code, Codex) have their
own tool chains and don't need a Python-side tool-calling loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


# ── Sandbox stdout line parser ────────────────────────────────────

# Patterns Claude Code emits during execution
_TOOL_CALL_RE = re.compile(
    r"^\s*(?:⚙|🔧|tool)\s*(?:call|using|running)[:\s]+(.+?)(?:\((.+?)\))?\s*$",
    re.IGNORECASE,
)
_FILE_MODIFIED_RE = re.compile(
    r"^\s*(?:📝|✏️|edit|wrote|modified|created|deleted)[:\s]+(.+?)\s*$",
    re.IGNORECASE,
)
_DIFF_RE = re.compile(r"^\s*```diff")
_THINKING_RE = re.compile(r"^\s*(?:💭|thinking)[:\s]", re.IGNORECASE)


def _parse_sandbox_line(line: str) -> tuple[str, dict[str, Any]] | None:
    """Parse a single sandbox stdout line into an event type + data.

    Returns None for uninteresting lines (plain text output, blank lines).
    Recognized patterns:
    - Tool calls: "⚙ ToolName(args)" → ("tool_call", {...})
    - File changes: "📝 path" → ("file_modified", {...})
    - Diff blocks: "```diff" → ("diff_start", {})
    - Thinking: "💭 ..." → ("thinking", {...})

    ponytail: regex-based — upgrade to structured JSON when Claude
    Code gains a streaming event protocol.
    """
    if not line.strip():
        return None

    # Tool call
    m = _TOOL_CALL_RE.match(line)
    if m:
        tool_name = m.group(1).strip()
        tool_args = m.group(2).strip() if m.group(2) else ""
        return ("tool_call", {
            "tool_name": tool_name,
            "args": tool_args[:200],
        })

    # File modification
    m = _FILE_MODIFIED_RE.match(line)
    if m:
        file_path = m.group(1).strip()
        return ("file_modified", {"file_path": file_path})

    # Diff block start
    if _DIFF_RE.match(line):
        return ("diff_start", {})

    # Thinking
    m = _THINKING_RE.match(line)
    if m:
        return ("thinking", {"snippet": line[:200]})

    return None


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

    def _dynamic_capacity(self, subtask: Subtask | None = None) -> int:
        """Return effective concurrency limit for this worker.

        Read-only subtasks (no file_constraints) can run more concurrently
        since they can't conflict. Write-heavy subtasks are limited.

        ponytail: simple heuristic — upgrade path is per-account locks.
        """
        base = self.max_capacity
        if subtask is None:
            return base
        # No file constraints → likely read-only → allow double concurrency
        if not subtask.file_constraints:
            return min(base * 2, 6)
        # Multiple file constraints → high conflict risk → limit to 1
        if len(subtask.file_constraints) >= 3:
            return max(1, base // 2)
        return base

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
        """Execute a subtask via sandbox agent.

        Includes checkpoint: saves intermediate result to engine memory
        before returning, and checks for existing checkpoint to allow
        resume-skip of already-completed subtasks.
        """
        self.current_task = subtask
        self._active_count += 1
        subtask.status = SubtaskStatus.IN_PROGRESS

        # Check for existing checkpoint — skip if already completed
        checkpoint = self._load_checkpoint(subtask.id)
        if checkpoint is not None and checkpoint.get("success"):
            logger.info(
                "Subtask %s has completed checkpoint, skipping execution",
                subtask.id[:8],
            )
            self.current_task = None
            self._active_count = max(0, self._active_count - 1)
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary=checkpoint.get("summary", "Resumed from checkpoint"),
                success=True,
            )

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

            # Save checkpoint for resume
            self._save_checkpoint(subtask.id, result)

            if result.success:
                comp_data: dict[str, Any] = {
                    "summary": result.summary[:300],
                    "success": True,
                    "modified_files": [
                        {"path": f.file_path, "type": f.change_type.value}
                        for f in (result.modified_files or [])
                    ],
                    "output": result.summary[:50000],  # ponytail: 50KB cap
                }
                await self._publish_event(
                    "subtask_completed",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data=comp_data,
                )
            else:
                # Build failure context: stderr tail + recent tool calls
                failure_data: dict[str, Any] = {
                    "error": result.summary[:300],
                    "worker_id": self.worker_id,
                }
                if result.stderr_tail:
                    failure_data["stderr_tail"] = result.stderr_tail
                if result.recent_tool_calls:
                    # Serialize as JSON string so it works through both
                    # HashMap<String, String> (local worker) and
                    # serde_json::Map<String, Value> (NATS) paths
                    failure_data["recent_tools"] = json.dumps(result.recent_tool_calls)
                await self._publish_event(
                    "subtask_failed",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data=failure_data,
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
                stderr_tail=str(e)[-2000:],
            )

        finally:
            self.current_task = None
            self._active_count = max(0, self._active_count - 1)

    def _save_checkpoint(self, subtask_id: str, result: SubtaskResult) -> None:
        """Persist subtask result to engine memory for checkpoint/resume.

        ponytail: synchronous write — engine.write_memory may be async,
        but we use in-memory fallback so it's fine. Non-fatal on failure.
        """
        if self.engine is None:
            return
        try:
            data = {
                "subtask_id": subtask_id,
                "worker_id": result.worker_id,
                "summary": result.summary,
                "success": result.success,
            }
            self.engine.write_memory(
                key_scope="checkpoint",
                key=f"subtask:{subtask_id}",
                content=json.dumps(data),
                content_type="structured",
                source_agent="worker",
            )
        except Exception:
            logger.debug("Failed to save checkpoint for subtask %s", subtask_id[:8])

    def _load_checkpoint(self, subtask_id: str) -> dict | None:
        """Load checkpoint from engine memory."""
        if self.engine is None:
            return None
        try:
            raw = self.engine.read_memory(
                key_scope="checkpoint",
                key=f"subtask:{subtask_id}",
            )
            if raw is not None:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            logger.debug("Failed to load checkpoint for subtask %s", subtask_id[:8])
        return None

    async def _execute_in_sandbox(self, subtask: Subtask) -> SubtaskResult:
        """Execute via sandbox (Claude Code / Codex).

        Automatically declares and releases EditIntent for file_constraints
        so the conflict detector tracks which files are being modified.
        """
        # Declare edit intent for conflict tracking
        declared_files: list[str] = []
        if subtask.file_constraints:
            from ultimate_coders.agent.conflict import EditIntent, EditType
            for fp in subtask.file_constraints:
                result, _ = self.conflict_detector.declare_intent(
                    EditIntent(
                        worker_id=self.worker_id,
                        file_path=fp,
                        edit_type=EditType.MODIFY,
                    )
                )
                if result.value != "no_conflict":
                    logger.warning(
                        "Conflict detected for %s: %s (proceeding anyway)",
                        fp, result.value,
                    )
                declared_files.append(fp)

        try:
            prompt = _SUBTASK_USER_TEMPLATE.format(
                description=subtask.description,
                expected_output=subtask.expected_output or "Complete the described task",
                file_constraints=", ".join(subtask.file_constraints) or "none",
            )

            # Streaming callback: parse each stdout line and emit events
            async def _on_stdout_line(line: str) -> None:
                """Parse sandbox stdout line and emit real-time events."""
                parsed = _parse_sandbox_line(line)
                if parsed is not None:
                    event_type, data = parsed
                    await self._publish_event(
                        event_type,
                        task_id=subtask.parent_id,
                        subtask_id=subtask.id,
                        data=data,
                    )

            output: AgentOutput = await self._sandbox_manager.execute(
                prompt, on_stdout_line=_on_stdout_line,
            )
            # ponytail: extract stderr_tail and recent tool calls for failure context
            stderr_tail = output.stderr_tail
            if not stderr_tail and hasattr(output, 'raw_stderr') and output.raw_stderr:
                stderr_tail = "\n".join(output.raw_stderr.strip().splitlines()[-10:])
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                modified_files=output.file_changes,
                summary=output.summary,
                success=output.success,
                stderr_tail=stderr_tail,
                recent_tool_calls=output.tool_calls[-5:],
            )
        finally:
            # Release edit intent after execution (success or failure)
            for fp in declared_files:
                self.conflict_detector.remove_intent(fp, self.worker_id)

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
