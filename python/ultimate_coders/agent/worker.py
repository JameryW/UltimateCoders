"""Worker — executes subtasks using LLM + tools.

Capabilities:
- Code generation and modification
- Code search and analysis
- Memory read/write for context
- Test execution
"""

from __future__ import annotations

import glob
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictResult,
    EditIntent,
    EditType,
    LineRange,
)
from ultimate_coders.agent.llm import (
    LLMClient,
    ToolCall,
    ToolDefinition,
    make_tool_definition,
)
from ultimate_coders.agent.rate_limiter import (
    CircuitBreaker,
    RateLimiter,
)
from ultimate_coders.agent.sandbox import (
    AgentOutput,
    SandboxConfig,
    SandboxManager,
)
from ultimate_coders.agent.types import (
    ChangeType,
    FileChange,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    WorkerInfo,
)

logger = logging.getLogger(__name__)

# System prompt for subtask execution
_WORKER_SYSTEM_PROMPT = """\
You are a coding assistant executing a subtask within a larger project.
You have access to tools for searching code, reading/writing memory, and
reading files. Use these tools to understand the codebase before making changes.

Workflow:
1. Search for relevant code and understand the existing structure
2. Read relevant files to understand the current implementation
3. Read memory for any relevant context or decisions
4. Implement the required changes
5. Write important decisions to memory for other workers

Be thorough in your research before making changes. Always read files
before modifying them. Store important findings in memory.
"""

_SUBTASK_USER_TEMPLATE = """\
Subtask: {description}

Expected output: {expected_output}

File constraints (do NOT modify): {file_constraints}

Context from previous subtasks:
{prior_context}
"""


class Worker:
    """Executes subtasks using LLM + tools.

    The Worker is the execution unit in the Orchestrator-Worker pattern.
    It receives assigned subtasks, executes them using LLM with tool
    calling, and reports results back.

    Usage:
        worker = Worker(worker_id="w1", engine=engine, llm_client=llm)
        result = await worker.execute_subtask(subtask)
    """

    def __init__(
        self,
        worker_id: str = "",
        engine: Any = None,
        llm_client: LLMClient | None = None,
        capabilities: list[str] | None = None,
        max_capacity: int = 3,
        conflict_detector: ConflictDetector | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        execution_mode: str = "sandbox",
        sandbox_config: SandboxConfig | None = None,
        event_emitter: Any | None = None,
    ):
        """Initialize the Worker.

        Args:
            worker_id: Unique worker identifier. Auto-generated if empty.
            engine: Engine instance for memory/search operations.
            llm_client: LLM client for subtask execution.
            capabilities: List of capability strings (e.g., "code", "search").
            max_capacity: Maximum concurrent subtasks.
            conflict_detector: Conflict detector for edit intent tracking.
            rate_limiter: Rate limiter for LLM API calls.
            circuit_breaker: Circuit breaker for LLM API fault tolerance.
            execution_mode: Execution mode ("sandbox" or "llm").
            sandbox_config: Configuration for sandbox execution
                (required if execution_mode="sandbox").
            event_emitter: Optional TaskEventEmitter for real-time dashboard
                event streaming. When set, the Worker emits subtask lifecycle
                and LLM interaction events during execution.
        """
        import uuid

        self.worker_id = worker_id or str(uuid.uuid4())
        self.engine = engine
        self.llm_client = llm_client
        self.capabilities = capabilities or ["code", "search", "memory", "test"]
        self.max_capacity = max_capacity
        self.current_task: Subtask | None = None
        self._active_count = 0
        self.tools = self._build_tools()
        self._tool_definitions = self._build_tool_definitions()
        self.conflict_detector = conflict_detector or ConflictDetector()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

        # Event emitter for real-time dashboard tracking
        self.event_emitter = event_emitter

        # Sandbox execution mode
        self.execution_mode = execution_mode
        if execution_mode == "sandbox":
            self._sandbox_config = sandbox_config or SandboxConfig()
            self._sandbox_manager = SandboxManager(self._sandbox_config, engine)
        else:
            self._sandbox_config = None
            self._sandbox_manager = None

    def get_info(self) -> WorkerInfo:
        """Get the current WorkerInfo for registration."""
        return WorkerInfo(
            id=self.worker_id,
            capabilities=self.capabilities,
            current_load=self._active_count,
            max_capacity=self.max_capacity,
        )

    async def execute_subtask(self, subtask: Subtask) -> SubtaskResult:
        """Execute a subtask using LLM + tools or sandbox mode.

        If execution_mode is "sandbox", delegates to the sandbox manager.
        Otherwise, implements the tool-calling loop:
        1. Build context from memory and search
        2. Create LLM prompt with subtask + context + tools
        3. Execute LLM with tool calling loop
        4. Collect results (modified files, summary)
        5. Return SubtaskResult

        Args:
            subtask: The subtask to execute.

        Returns:
            SubtaskResult with execution outcome.
        """
        self.current_task = subtask
        self._active_count += 1
        subtask.status = SubtaskStatus.IN_PROGRESS

        # Emit subtask started event
        if self.event_emitter is not None:
            await self.event_emitter.emit(
                "subtask_started",
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                data={"description": subtask.description, "worker_id": self.worker_id},
            )

        try:
            if self.execution_mode == "sandbox":
                result = await self._execute_in_sandbox(subtask)
            else:
                # Default: LLM tool-calling loop
                result = await self._execute_with_llm(subtask)

            # Emit subtask completed/failed event based on result
            if self.event_emitter is not None:
                if result.success:
                    await self.event_emitter.emit(
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
                    await self.event_emitter.emit(
                        "subtask_failed",
                        task_id=subtask.parent_id,
                        subtask_id=subtask.id,
                        data={"error": result.summary[:300], "worker_id": self.worker_id},
                    )
            return result

        except Exception as e:
            logger.error(
                "Subtask %s execution failed: %s",
                subtask.id,
                e,
                exc_info=True,
            )
            # Emit subtask failed event
            if self.event_emitter is not None:
                await self.event_emitter.emit(
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

    async def _execute_with_llm(self, subtask: Subtask) -> SubtaskResult:
        """Execute a subtask using LLM + tools (existing tool-calling loop).

        Args:
            subtask: The subtask to execute.

        Returns:
            SubtaskResult with execution outcome.
        """
        # Gather prior context from completed dependencies
        prior_context = await self._gather_prior_context(subtask)

        # Build messages
        messages = self._build_messages(subtask, prior_context)

        # Execute LLM with tool calling loop
        if self.llm_client is None:
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary="No LLM client available",
                success=False,
            )

        # Emit llm_request event before calling the LLM
        if self.event_emitter is not None:
            messages_summary = []
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, str):
                    messages_summary.append({"role": role, "content_preview": content[:100]})
                elif isinstance(content, list):
                    messages_summary.append({"role": role, "content_preview": str(content)[:100]})
                else:
                    messages_summary.append({"role": role, "content_preview": str(content)[:100]})
            await self.event_emitter.emit(
                "llm_request",
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                data={
                    "model": self.llm_client.model,
                    "messages_summary": messages_summary,
                },
            )

        # Build on_tool_call callback for event emitter
        async def _on_tool_call(tool_name: str, tool_input: dict, result: str) -> None:
            if self.event_emitter is not None:
                await self.event_emitter.emit(
                    "tool_call",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data={"tool": tool_name, "input_summary": str(tool_input)[:200]},
                )
                # Truncate result for streaming
                result_summary = result[:500] if len(result) > 500 else result
                await self.event_emitter.emit(
                    "tool_result",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data={"tool": tool_name, "result_summary": result_summary},
                )

        response, tool_log = await self.llm_client.complete_with_tools(
            messages=messages,
            tools=self._tool_definitions,
            system=_WORKER_SYSTEM_PROMPT,
            max_tokens=4096,
            tool_executor=self._execute_tool,
            on_tool_call=_on_tool_call if self.event_emitter else None,
        )

        # Build result
        modified_files = self._collect_modified_files(tool_log)
        summary = response.text or "Subtask completed"

        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id=self.worker_id,
            modified_files=modified_files,
            summary=summary,
            success=True,
        )

    async def _execute_in_sandbox(self, subtask: Subtask) -> SubtaskResult:
        """Execute a subtask using the sandbox agent executor.

        Runs the coding agent (Claude Code or Codex) in an isolated
        sandbox environment with resource limits and file change tracking.

        Args:
            subtask: The subtask to execute.

        Returns:
            SubtaskResult with execution outcome.
        """
        if self._sandbox_manager is None:
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary="Sandbox manager not configured (set execution_mode='sandbox')",
                success=False,
            )

        # Build the prompt from subtask description
        prompt = _SUBTASK_USER_TEMPLATE.format(
            description=subtask.description,
            expected_output=subtask.expected_output or "Complete the described task",
            file_constraints=", ".join(subtask.file_constraints) or "none",
            prior_context="(sandbox mode: prior context not gathered)",
        )

        # Execute in sandbox
        output: AgentOutput = await self._sandbox_manager.execute(prompt)

        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id=self.worker_id,
            modified_files=output.file_changes,
            summary=output.summary,
            success=output.success,
        )

    async def send_heartbeat(self) -> dict[str, Any]:
        """Send heartbeat to indicate the worker is alive.

        Returns:
            Dict with worker status information.
        """
        return {
            "worker_id": self.worker_id,
            "capabilities": self.capabilities,
            "current_load": self._active_count,
            "max_capacity": self.max_capacity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── Tool implementations ────────────────────────────────────

    async def _tool_search(self, query: str, **kwargs: Any) -> str:
        """Tool: Search code across indexed repositories.

        Args:
            query: Search query string.

        Returns:
            JSON string with search results.
        """
        if self.engine is None:
            return json.dumps({"error": "No engine available"})

        try:
            from ultimate_coders.search import SearchQuery

            search_query = SearchQuery(query).limit(kwargs.get("max_results", 10))
            if kwargs.get("repo_ids"):
                search_query.in_repos(kwargs["repo_ids"])
            if kwargs.get("languages"):
                search_query.in_languages(kwargs["languages"])

            result = self.engine.search(search_query)

            # Convert to serializable format
            items = []
            if result and hasattr(result, "items"):
                for item in result.items[:10]:
                    items.append(
                        {
                            "repo_id": getattr(item, "repo_id", ""),
                            "file_path": getattr(item, "file_path", ""),
                            "start_line": getattr(item, "start_line", 0),
                            "content_snippet": getattr(item, "content_snippet", "")[:200],
                            "score": getattr(item, "score", 0.0),
                        }
                    )

            return json.dumps({"results": items, "count": len(items)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_read_memory(
        self,
        key_scope: str,
        key: str,
        **kwargs: Any,
    ) -> str:
        """Tool: Read from memory.

        Args:
            key_scope: "task", "project", or "global".
            key: The memory key name.

        Returns:
            JSON string with memory content or error.
        """
        if self.engine is None:
            return json.dumps({"error": "No engine available"})

        try:
            entry = self.engine.read_memory(
                key_scope=key_scope,
                key=key,
                task_id=kwargs.get("task_id"),
                project_id=kwargs.get("project_id"),
            )
            if entry is None:
                return json.dumps({"found": False})

            # Extract content from entry
            content = getattr(entry, "content", None)
            if content:
                text = getattr(content, "text", None) or str(content)
            else:
                text = str(entry)

            return json.dumps({"found": True, "content": text})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_write_memory(
        self,
        key_scope: str,
        key: str,
        content: str,
        **kwargs: Any,
    ) -> str:
        """Tool: Write to memory.

        Args:
            key_scope: "task", "project", or "global".
            key: The memory key name.
            content: Content to store.

        Returns:
            JSON string with success status.
        """
        if self.engine is None:
            return json.dumps({"error": "No engine available"})

        try:
            self.engine.write_memory(
                key_scope=key_scope,
                key=key,
                content=content,
                content_type=kwargs.get("content_type", "text"),
                source_agent=self.worker_id,
                importance=kwargs.get("importance", 0.5),
                task_id=kwargs.get("task_id"),
                project_id=kwargs.get("project_id"),
                language=kwargs.get("language"),
                file_path=kwargs.get("file_path"),
                uri=kwargs.get("uri"),
                description=kwargs.get("description"),
            )
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_read_file(self, file_path: str) -> str:
        """Tool: Read a file from the local filesystem.

        Args:
            file_path: Path to the file to read.

        Returns:
            File content as a string, or error message.
        """
        try:
            if not os.path.isfile(file_path):
                return json.dumps({"error": f"File not found: {file_path}"})

            # Limit file size to avoid overwhelming the context
            max_bytes = 100_000  # 100KB
            with open(file_path, encoding="utf-8", errors="replace") as f:
                content = f.read(max_bytes)
                if len(content) == max_bytes:
                    content += "\n... (truncated)"

            return json.dumps(
                {
                    "file_path": file_path,
                    "content": content,
                    "size": os.path.getsize(file_path),
                }
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_list_files(
        self,
        directory: str = ".",
        pattern: str = "**/*",
    ) -> str:
        """Tool: List files in a directory.

        Args:
            directory: Directory to list (default: current directory).
            pattern: Glob pattern (default: all files).

        Returns:
            JSON string with file list.
        """
        try:
            if not os.path.isdir(directory):
                return json.dumps({"error": f"Directory not found: {directory}"})

            matches = glob.glob(
                os.path.join(directory, pattern),
                recursive=True,
            )

            # Filter out hidden dirs and common non-code dirs
            skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "target"}
            filtered = []
            for m in matches[:200]:  # Limit results
                parts = m.split(os.sep)
                if any(p in skip_dirs for p in parts):
                    continue
                filtered.append(m)

            return json.dumps({"files": filtered, "count": len(filtered)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── Private helpers ─────────────────────────────────────────

    def _build_tools(self) -> dict[str, Callable]:
        """Build available tools for LLM function calling."""
        return {
            "search": self._tool_search,
            "read_memory": self._tool_read_memory,
            "write_memory": self._tool_write_memory,
            "read_file": self._tool_read_file,
            "list_files": self._tool_list_files,
        }

    def _build_tool_definitions(self) -> list[ToolDefinition]:
        """Build tool definitions for the LLM API."""
        return [
            make_tool_definition(
                name="search",
                description=(
                    "Search code across indexed repositories. Returns matching code snippets."
                ),
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Search query string",
                    },
                },
            ),
            make_tool_definition(
                name="read_memory",
                description=(
                    "Read from the shared memory system. Memory stores "
                    "task context, decisions, and project knowledge."
                ),
                parameters={
                    "key_scope": {
                        "type": "string",
                        "description": 'Memory scope: "task", "project", or "global"',
                    },
                    "key": {
                        "type": "string",
                        "description": "The memory key name",
                    },
                },
            ),
            make_tool_definition(
                name="write_memory",
                description=(
                    "Write to the shared memory system. Use this to store "
                    "findings, decisions, and context for other agents."
                ),
                parameters={
                    "key_scope": {
                        "type": "string",
                        "description": 'Memory scope: "task", "project", or "global"',
                    },
                    "key": {
                        "type": "string",
                        "description": "The memory key name",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to store",
                    },
                },
            ),
            make_tool_definition(
                name="read_file",
                description=(
                    "Read a file from the local filesystem. Use this to "
                    "understand existing code before making changes."
                ),
                parameters={
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file",
                    },
                },
            ),
            make_tool_definition(
                name="list_files",
                description=(
                    "List files in a directory using a glob pattern. "
                    "Use this to explore the project structure."
                ),
                parameters={
                    "directory": {
                        "type": "string",
                        "description": "Directory to list (default: current directory)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (default: **/*)",
                    },
                },
            ),
        ]

    async def _execute_tool(self, tool_call: ToolCall) -> str:
        """Execute a tool call from the LLM.

        Args:
            tool_call: The tool call to execute.

        Returns:
            String result of the tool execution.
        """
        tool_fn = self.tools.get(tool_call.name)
        if tool_fn is None:
            return json.dumps({"error": f"Unknown tool: {tool_call.name}"})

        try:
            result = await tool_fn(**tool_call.input)
            return result
        except Exception as e:
            return json.dumps({"error": f"Tool execution error: {e}"})

    async def _gather_prior_context(self, subtask: Subtask) -> str:
        """Gather context from completed dependency subtasks.

        Args:
            subtask: The subtask whose dependencies to check.

        Returns:
            Formatted context string from prior subtask results.
        """
        if self.engine is None or not subtask.depends_on:
            return "(no prior context)"

        parts = []
        for dep_id in subtask.depends_on:
            try:
                entry = self.engine.read_memory(
                    key_scope="task",
                    key=f"result_{dep_id}",
                    task_id=subtask.parent_id,
                )
                if entry is not None:
                    content = getattr(entry, "content", None)
                    if content:
                        text = getattr(content, "text", None) or str(content)
                    else:
                        text = str(entry)
                    parts.append(f"Subtask {dep_id}: {text[:300]}")
            except Exception:
                logger.debug("Failed to read context for dep %s", dep_id, exc_info=True)

        if parts:
            return "\n".join(parts)
        return "(no prior context found)"

    def _build_messages(
        self,
        subtask: Subtask,
        prior_context: str,
    ) -> list[dict[str, Any]]:
        """Build the LLM messages for subtask execution.

        Args:
            subtask: The subtask to execute.
            prior_context: Context from completed dependencies.

        Returns:
            List of message dicts for the LLM.
        """
        user_content = _SUBTASK_USER_TEMPLATE.format(
            description=subtask.description,
            expected_output=subtask.expected_output or "Complete the described task",
            file_constraints=", ".join(subtask.file_constraints) or "none",
            prior_context=prior_context,
        )

        return [{"role": "user", "content": user_content}]

    def _collect_modified_files(
        self,
        tool_log: list[dict[str, Any]],
    ) -> list[FileChange]:
        """Extract file modifications from the tool call log.

        Looks for write_file or edit_file tool calls in the log
        and collects them as FileChange objects.

        Args:
            tool_log: Log of tool calls and results.

        Returns:
            List of FileChange objects.
        """
        # For now, we track files that were read (which implies interest)
        # Actual file modification tracking would come from a write_file tool
        # or from the worker's execution environment
        modified = []
        for entry in tool_log:
            tool_name = entry.get("tool_call", {}).get("name", "")
            tool_input = entry.get("tool_call", {}).get("input", {})

            if tool_name == "read_file":
                file_path = tool_input.get("file_path", "")
                if file_path:
                    modified.append(
                        FileChange(
                            file_path=file_path,
                            change_type=ChangeType.MODIFIED,
                            diff="",  # Actual diff would come from write_file
                        )
                    )

        return modified

    # ── Fault Tolerance Methods ────────────────────────────────────

    def declare_edit_intent(
        self,
        file_path: str,
        edit_type: EditType = EditType.MODIFY,
        regions: list[tuple[int, int]] | None = None,
    ) -> tuple[ConflictResult, dict | None]:
        """Declare an intent to edit a file.

        Should be called before modifying a file to check for conflicts
        with other workers.

        Args:
            file_path: Path to the file being edited.
            edit_type: Type of edit (create, modify, delete).
            regions: List of (start, end) line range tuples.

        Returns:
            A tuple of (ConflictResult, optional conflict info dict).
        """
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
                file_path,
                result.value,
                info.conflicting_workers if info else [],
            )

        return result, info

    def release_edit_intent(self, file_path: str) -> None:
        """Release an edit intent after the edit is completed.

        Args:
            file_path: Path to the file that was edited.
        """
        self.conflict_detector.remove_intent(file_path, self.worker_id)

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
