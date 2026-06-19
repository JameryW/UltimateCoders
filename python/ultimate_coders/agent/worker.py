"""Worker — executes subtasks using LLM + tools.

Capabilities:
- Code generation and modification
- Code search and analysis
- Memory read/write for context
- Test execution
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable

from ultimate_coders.agent.codegraph import CodegraphClient
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
    AdaptationStrategy,
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

        # Codegraph knowledge graph client (graceful degradation)
        cg_path = sandbox_config.project_path if sandbox_config else ""
        self._codegraph = CodegraphClient(cg_path)

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
            # Wrap execution with timeout
            timeout_secs = subtask.timeout_seconds or 600  # 10 min default
            try:
                if self.execution_mode == "sandbox":
                    result = await asyncio.wait_for(
                        self._execute_in_sandbox(subtask),
                        timeout=timeout_secs,
                    )
                else:
                    # Default: LLM tool-calling loop
                    result = await asyncio.wait_for(
                        self._execute_with_llm(subtask),
                        timeout=timeout_secs,
                    )
            except asyncio.TimeoutError:
                result = SubtaskResult(
                    subtask_id=subtask.id,
                    worker_id=self.worker_id,
                    summary=f"Subtask timed out after {timeout_secs}s",
                    success=False,
                )

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
            # Adaptive retry: classify error and try once with adapted strategy
            try:
                adapted_result = await self._adaptive_retry(subtask, e)
                if self.event_emitter is not None:
                    await self.event_emitter.emit(
                        "subtask_adapted",
                        task_id=subtask.parent_id,
                        subtask_id=subtask.id,
                        data={
                            "original_error": str(e),
                            "adaptation": adapted_result.adaptation_strategy.value,
                            "success": adapted_result.success,
                        },
                    )
                return adapted_result
            except Exception as adapt_err:
                logger.error(
                    "Subtask %s adaptive retry also failed: %s",
                    subtask.id, adapt_err, exc_info=True,
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
                    summary=f"Execution error: {e} (adaptation also failed: {adapt_err})",
                    success=False,
                )

        finally:
            self.current_task = None
            self._active_count = max(0, self._active_count - 1)

    async def _execute_with_llm(self, subtask: Subtask, *, max_tokens: int = 4096) -> SubtaskResult:
        """Execute a subtask using LLM + tools (existing tool-calling loop).

        Args:
            subtask: The subtask to execute.
            max_tokens: Maximum tokens for LLM response (default 4096).

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

        # Circuit breaker check before LLM call
        if not self.circuit_breaker.allow_request():
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary="Circuit breaker open — LLM requests temporarily blocked",
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

        try:
            response, tool_log = await self.llm_client.complete_with_tools(
                messages=messages,
                tools=self._tool_definitions,
                system=_WORKER_SYSTEM_PROMPT,
                max_tokens=max_tokens,
                tool_executor=self._execute_tool,
                on_tool_call=_on_tool_call if self.event_emitter else None,
            )
        except Exception:
            self.circuit_breaker.record_failure()
            raise
        self.circuit_breaker.record_success()

        # Build result
        modified_files = self._collect_modified_files(tool_log)
        summary = response.text or "Subtask completed"

        # Self-reflection: quick quality check
        confidence = await self._self_evaluate(subtask, summary, modified_files, tool_log)

        # Write execution experience to memory for future reference
        await self._record_experience(subtask, summary, confidence, modified_files)

        if confidence < 0.5 and self.llm_client is not None:
            # Low confidence → mark as failed so Orchestrator re-decomposes
            summary = f"[⚠️ low confidence: {confidence:.0%}] {summary}"
            if self.event_emitter is not None:
                await self.event_emitter.emit(
                    "subtask_low_confidence",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data={"confidence": confidence},
                )
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                modified_files=modified_files,
                summary=summary,
                success=False,  # trigger re-decompose via Orchestrator retry
            )
        elif confidence < 0.7 and self.llm_client is not None:
            # Medium confidence → add verification step annotation
            summary = f"[~ confidence: {confidence:.0%}, verify recommended] {summary}"
            if self.event_emitter is not None:
                await self.event_emitter.emit(
                    "subtask_medium_confidence",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data={"confidence": confidence},
                )

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

        # Build codegraph context for sandbox prompt (pre-processing layer)
        codegraph_context = ""
        if self._codegraph.is_available():
            try:
                codegraph_context = self._codegraph.explore(subtask.description, max_nodes=10)
            except Exception:
                logger.debug("Codegraph explore failed for sandbox subtask", exc_info=True)

        # Build the prior context string
        prior_context = (
            codegraph_context if codegraph_context else "(sandbox mode: prior context not gathered)"
        )

        # Build the prompt from subtask description
        prompt = _SUBTASK_USER_TEMPLATE.format(
            description=subtask.description,
            expected_output=subtask.expected_output or "Complete the described task",
            file_constraints=", ".join(subtask.file_constraints) or "none",
            prior_context=prior_context,
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

    async def _self_evaluate(
        self,
        subtask: Subtask,
        summary: str,
        modified_files: list[FileChange],
        tool_log: list[dict[str, Any]] | None = None,
    ) -> float:
        """Quick self-evaluation of execution quality.

        Returns a confidence score 0.0–1.0 based on:
        - Were any files modified? (higher confidence if yes)
        - Does the summary mention key expected_output terms?
        - Was a run_command tool used for verification?
        - Does the summary contain error keywords? (penalty)
        - Empty result: no files modified and short summary? (penalty)

        ponytail: heuristic check, no extra LLM call. Upgrade to LLM-based
        reflection if the heuristic proves insufficient.
        """
        score = 0.5  # baseline

        # Files modified → higher confidence
        if modified_files:
            score += 0.2

        # Summary addresses expected output keywords
        expected = subtask.expected_output or ""
        if expected:
            # Check if key nouns from expected_output appear in summary
            keywords = set(re.findall(r"\b\w{4,}\b", expected.lower()))
            summary_words = set(re.findall(r"\b\w{4,}\b", summary.lower()))
            overlap = len(keywords & summary_words)
            if keywords and overlap > 0:
                score += min(0.2, overlap / len(keywords) * 0.2)

        # Verification step: run_command used → +0.1
        if tool_log:
            for entry in tool_log:
                name = entry.get("tool_call", {}).get("name", "")
                if name == "run_command":
                    score += 0.1
                    break

        # Error keywords in summary → -0.15
        summary_lower = summary.lower()
        error_markers = {"error", "failed", "exception", "traceback", "not found"}
        if any(m in summary_lower for m in error_markers):
            score -= 0.15

        # Empty result: no files + short summary → -0.1
        if not modified_files and len(summary) < 50:
            score -= 0.1

        return max(0.0, min(1.0, score))

    async def _record_experience(
        self,
        subtask: Subtask,
        summary: str,
        confidence: float,
        modified_files: list[FileChange],
    ) -> None:
        """Write execution experience to memory for future subtasks.

        ponytail: best-effort write, failures are non-fatal. Experience
        helps later subtasks avoid repeating the same mistakes.
        """
        if self.engine is None:
            return
        try:
            experience = json.dumps({
                "subtask_id": subtask.id,
                "description": subtask.description[:200],
                "confidence": round(confidence, 2),
                "files_modified": [f.file_path for f in (modified_files or [])],
                "summary": summary[:500],
            })
            self.engine.write_memory(
                key_scope="task",
                key=f"experience_{subtask.id}",
                content=experience,
                content_type="structured",
                source_agent=self.worker_id,
                importance=0.6 if confidence >= 0.5 else 0.8,
                task_id=subtask.parent_id,
            )
        except Exception:
            logger.debug("Failed to write experience to memory", exc_info=True)

    # ── Adaptive decision ──────────────────────────────────────

    @staticmethod
    def _classify_error(error: Exception) -> AdaptationStrategy:
        """Classify an execution error into an adaptation strategy.

        ponytail: pattern matching on error type/message. Simple,
        covers the 4 main failure modes. Expand when new patterns emerge.
        """
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()

        # Timeout → shrink scope
        if (
            isinstance(error, asyncio.TimeoutError)
            or "timeout" in error_str
            or "timed out" in error_str
        ):
            return AdaptationStrategy.SHRINK_SCOPE

        # Tool/engine unavailable → fallback or pure LLM
        if (
            "no module" in error_str
            or "not found" in error_str
            or "not available" in error_str
            or "no engine" in error_str
            or "importerror" in error_type
        ):
            if "engine" in error_str or "module" in error_str:
                return AdaptationStrategy.PURE_LLM
            return AdaptationStrategy.FALLBACK_TOOL

        # Conflict → wait and retry
        if "conflict" in error_str or "conflicted" in error_str:
            return AdaptationStrategy.WAIT_RETRY

        # Default: unknown error → pure LLM fallback
        return AdaptationStrategy.PURE_LLM

    async def _adaptive_retry(
        self,
        subtask: Subtask,
        error: Exception,
    ) -> SubtaskResult:
        """Retry a subtask with an adapted strategy based on error classification.

        Args:
            subtask: The subtask that failed.
            error: The exception that caused the failure.

        Returns:
            SubtaskResult with adaptation_strategy set.
        """
        strategy = self._classify_error(error)
        logger.info(
            "Subtask %s failed with %s, adapting: %s",
            subtask.id, type(error).__name__, strategy.value,
        )

        if strategy == AdaptationStrategy.SHRINK_SCOPE:
            # Reduce timeout and max_tokens, retry with tighter scope
            reduced_timeout = (subtask.timeout_seconds or 600) // 2
            subtask.timeout_seconds = max(reduced_timeout, 60)
            result = await self._execute_with_llm(subtask, max_tokens=2048)
            result.adaptation_strategy = strategy
            if not result.success:
                result.summary = f"[adapted: shrink_scope, still failed] {result.summary}"
            else:
                result.summary = f"[adapted: shrink_scope] {result.summary}"
            return result

        if strategy == AdaptationStrategy.PURE_LLM:
            # Skip tools, use LLM completion only
            if self.llm_client is None:
                return SubtaskResult(
                    subtask_id=subtask.id,
                    worker_id=self.worker_id,
                    summary=f"Cannot adapt (no LLM client): {error}",
                    success=False,
                    adaptation_strategy=strategy,
                )
            prior_context = await self._gather_prior_context(subtask)
            messages = self._build_messages(subtask, prior_context)
            # Remove tools — pure text completion
            try:
                response = await self.llm_client.complete(
                    messages=messages,
                    system=_WORKER_SYSTEM_PROMPT,
                    max_tokens=2048,
                    temperature=0.3,
                )
            except Exception:
                self.circuit_breaker.record_failure()
                raise
            self.circuit_breaker.record_success()
            summary = response.text or "Pure LLM fallback completed"
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary=f"[adapted: pure_llm] {summary}",
                success=True,
                adaptation_strategy=strategy,
            )

        if strategy == AdaptationStrategy.WAIT_RETRY:
            # Wait 2s then retry (conflict may have resolved)
            await asyncio.sleep(2)
            result = await self._execute_with_llm(subtask)
            result.adaptation_strategy = strategy
            if not result.success:
                result.summary = f"[adapted: wait_retry, still failed] {result.summary}"
            else:
                result.summary = f"[adapted: wait_retry] {result.summary}"
            return result

        if strategy == AdaptationStrategy.FALLBACK_TOOL:
            # Retry with reduced tool set — remove codegraph tools
            codegraph_tools = {
                "symbol_search", "find_callers", "find_callees",
                "impact_analysis", "explore_code",
            }
            original_tools = self.tools
            original_defs = self._tool_definitions
            self.tools = {
                k: v for k, v in self.tools.items()
                if k not in codegraph_tools
            }
            self._tool_definitions = [
                d for d in self._tool_definitions
                if d.name not in codegraph_tools
            ]
            try:
                result = await self._execute_with_llm(subtask)
            finally:
                # Restore original tool set
                self.tools = original_tools
                self._tool_definitions = original_defs
            result.adaptation_strategy = strategy
            result.summary = f"[adapted: fallback_tool] {result.summary}"
            return result

        # Should not reach here
        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id=self.worker_id,
            summary=f"Unknown adaptation strategy: {error}",
            success=False,
            adaptation_strategy=strategy,
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

    async def _tool_edit_file(
        self,
        file_path: str,
        content: str,
        create: bool = False,
        **kwargs: Any,
    ) -> str:
        """Tool: Write content to a file atomically.

        When create=False (default), the file must already exist; it will be
        overwritten.  When create=True, the file must NOT exist; it will be
        created as a new file.

        The write is performed atomically by writing to a temporary file first
        and then renaming it over the target.

        Args:
            file_path: Path to the file to write.
            content: Content to write.
            create: If True, create a new file (error if exists).
                    If False, overwrite existing file (error if not found).

        Returns:
            JSON string with success status and bytes written.
        """
        try:
            # Validate UTF-8 content
            try:
                content.encode("utf-8")
            except UnicodeEncodeError as exc:
                return json.dumps({"error": f"Content is not valid UTF-8: {exc}"})

            abs_path = os.path.abspath(file_path)

            if create:
                # create=True: file must NOT exist
                if os.path.exists(abs_path):
                    return json.dumps(
                        {"error": f"File already exists (use create=False to overwrite): "
                                  f"{abs_path}"}
                    )
            else:
                # create=False: file MUST exist
                if not os.path.isfile(abs_path):
                    return json.dumps(
                        {"error": f"File not found (use create=True for new files): {abs_path}"}
                    )

            # Ensure parent directory exists
            parent_dir = os.path.dirname(abs_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            # Atomic write: temp file then rename
            fd, tmp_path = tempfile.mkstemp(
                dir=parent_dir,
                prefix=".uc_edit_",
                suffix=".tmp",
            )
            try:
                encoded = content.encode("utf-8")
                with os.fdopen(fd, "wb") as tmp_f:
                    tmp_f.write(encoded)
                os.replace(tmp_path, abs_path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            return json.dumps(
                {
                    "success": True,
                    "file_path": abs_path,
                    "bytes_written": len(encoded),
                }
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_search_memory(
        self,
        query: str,
        scope_type: str = "all",
        project_id: str | None = None,
        max_results: int = 10,
        **kwargs: Any,
    ) -> str:
        """Tool: Search long-term memory semantically.

        Args:
            query: Search query text.
            scope_type: Search scope — "project", "global", or "all".
            project_id: Project ID for project-scoped search.
            max_results: Maximum number of results (default 10).

        Returns:
            JSON string with search results.
        """
        if self.engine is None:
            return json.dumps({"error": "No engine available"})

        try:
            results = self.engine.search_memory(
                query=query,
                scope_type=scope_type,
                project_id=project_id,
                max_results=max_results,
            )

            items = []
            for r in results:
                content = getattr(r, "content", None) or getattr(r, "entry", None)
                if content:
                    text = getattr(content, "text", None) or str(content)
                else:
                    text = str(r)
                items.append(
                    {
                        "key": getattr(r, "key", ""),
                        "scope": getattr(r, "scope", ""),
                        "content": text[:500],
                        "score": getattr(r, "score", 0.0),
                    }
                )

            return json.dumps({"results": items, "count": len(items)})
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

    async def _tool_symbol_search(self, query: str, kind: str = "", **kwargs: Any) -> str:
        """Tool: Search for code symbols in the knowledge graph.

        Args:
            query: Symbol name or pattern to search for.
            kind: Optional kind filter (function, method, class, etc.).

        Returns:
            JSON string with symbol search results.
        """
        if not self._codegraph.is_available():
            return json.dumps({"error": "Codegraph not available"})
        try:
            results = self._codegraph.search(query, kind=kind or None)
            return json.dumps(results, indent=2) if results else json.dumps({"results": []})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_find_callers(self, symbol: str, **kwargs: Any) -> str:
        """Tool: Find all callers of a symbol.

        Args:
            symbol: Name of the function/method to find callers for.

        Returns:
            JSON string with caller information.
        """
        if not self._codegraph.is_available():
            return json.dumps({"error": "Codegraph not available"})
        try:
            results = self._codegraph.callers(symbol)
            return json.dumps(results, indent=2) if results else json.dumps({"callers": []})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_find_callees(self, symbol: str, **kwargs: Any) -> str:
        """Tool: Find what a symbol calls.

        Args:
            symbol: Name of the function/method to find callees for.

        Returns:
            JSON string with callee information.
        """
        if not self._codegraph.is_available():
            return json.dumps({"error": "Codegraph not available"})
        try:
            results = self._codegraph.callees(symbol)
            return json.dumps(results, indent=2) if results else json.dumps({"callees": []})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_impact_analysis(self, symbol: str, **kwargs: Any) -> str:
        """Tool: Analyze impact of changing a symbol.

        Args:
            symbol: Name of the symbol to analyze impact for.

        Returns:
            JSON string with impact analysis results.
        """
        if not self._codegraph.is_available():
            return json.dumps({"error": "Codegraph not available"})
        try:
            results = self._codegraph.impact(symbol)
            return json.dumps(
                {"symbol": symbol, "affected_count": len(results), "affected": results},
                indent=2,
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_explore_code(self, query: str, **kwargs: Any) -> str:
        """Tool: Explore code structure for a natural language query.

        Args:
            query: Natural language query about the codebase.

        Returns:
            Markdown string with structured code context.
        """
        if not self._codegraph.is_available():
            return "Codegraph not available"
        try:
            result = self._codegraph.explore(query)
            return result or "No codegraph results found for this query"
        except Exception as e:
            return f"Codegraph explore error: {e}"

    async def _tool_run_command(
        self,
        command: str,
        timeout: int = 120,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Tool: Execute a shell command and return output.

        Runs the command as a subprocess with a timeout. Use this for
        running tests, linters, build commands, git operations, etc.

        Args:
            command: Shell command to execute.
            timeout: Maximum execution time in seconds (default 120).
            cwd: Working directory (defaults to project root).

        Returns:
            JSON string with exit_code, stdout, stderr.
        """
        try:
            work_dir = cwd or os.getcwd()
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return json.dumps({
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout}s",
                    "timed_out": True,
                })

            stdout = stdout_bytes.decode("utf-8", errors="replace")[:50_000]
            stderr = stderr_bytes.decode("utf-8", errors="replace")[:10_000]
            return json.dumps({
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": False,
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _tool_apply_diff(
        self,
        file_path: str,
        diff: str,
        **kwargs: Any,
    ) -> str:
        """Tool: Apply a unified diff patch to a file.

        Applies the diff using the standard patch algorithm. Supports
        context lines for precise matching. If a hunk fails to apply,
        returns an error for that hunk without modifying the file.

        Args:
            file_path: Path to the file to patch.
            diff: Unified diff content to apply.

        Returns:
            JSON string with success status and hunks applied.
        """
        try:
            abs_path = os.path.abspath(file_path)
            if not os.path.isfile(abs_path):
                return json.dumps({"error": f"File not found: {abs_path}"})

            with open(abs_path, encoding="utf-8", errors="replace") as f:
                original = f.read()

            patched, hunks_applied, errors = _apply_unified_diff(original, diff)
            if errors:
                return json.dumps({
                    "error": f"Failed to apply {len(errors)} hunk(s)",
                    "hunks_applied": hunks_applied,
                    "hunk_errors": errors,
                })

            # Atomic write
            parent_dir = os.path.dirname(abs_path)
            fd, tmp_path = tempfile.mkstemp(
                dir=parent_dir, prefix=".uc_diff_", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
                    tmp_f.write(patched)
                os.replace(tmp_path, abs_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            return json.dumps({
                "success": True,
                "file_path": abs_path,
                "hunks_applied": hunks_applied,
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── Private helpers ─────────────────────────────────────────

    def _build_tools(self) -> dict[str, Callable]:
        """Build available tools for LLM function calling."""
        return {
            "search": self._tool_search,
            "read_memory": self._tool_read_memory,
            "write_memory": self._tool_write_memory,
            "edit_file": self._tool_edit_file,
            "search_memory": self._tool_search_memory,
            "read_file": self._tool_read_file,
            "list_files": self._tool_list_files,
            "symbol_search": self._tool_symbol_search,
            "find_callers": self._tool_find_callers,
            "find_callees": self._tool_find_callees,
            "impact_analysis": self._tool_impact_analysis,
            "explore_code": self._tool_explore_code,
            "run_command": self._tool_run_command,
            "apply_diff": self._tool_apply_diff,
        }

    def _build_tool_definitions(self) -> list[ToolDefinition]:
        """Build tool definitions for the LLM API.

        Each tool has a precise description, required markers, and enum
        constraints so the LLM can choose and call tools accurately.
        """
        return [
            make_tool_definition(
                name="search",
                description=(
                    "Full-text search across indexed repositories. Returns "
                    "matching code snippets with file path, line number, and "
                    "relevance score. Use for finding where a concept, API, or "
                    "pattern appears in the codebase."
                ),
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Search query — keywords, class/function names, or phrases",
                        "required": True,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 10, max 50)",
                    },
                    "repo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict search to these repository IDs",
                    },
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to these languages (e.g. ['python', 'rust'])",
                    },
                },
            ),
            make_tool_definition(
                name="read_memory",
                description=(
                    "Read a value from the shared layered memory system. "
                    "Memory stores task context, design decisions, and project "
                    "knowledge. Reads are scoped: 'task' for current task, "
                    "'project' for project-wide, 'global' for cross-project."
                ),
                parameters={
                    "key_scope": {
                        "type": "string",
                        "description": "Memory scope",
                        "enum": ["task", "project", "global"],
                        "required": True,
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "The memory key name (e.g. 'task_definition', "
                            "'design_decisions')"
                        ),
                        "required": True,
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID (required when key_scope='task')",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Project ID (required when key_scope='project')",
                    },
                },
            ),
            make_tool_definition(
                name="write_memory",
                description=(
                    "Write a value to the shared layered memory system. "
                    "Use this to persist findings, decisions, and context "
                    "so other agents can access them. Choose scope carefully: "
                    "'task' for subtask results, 'project' for design decisions, "
                    "'global' for cross-project knowledge."
                ),
                parameters={
                    "key_scope": {
                        "type": "string",
                        "description": "Memory scope",
                        "enum": ["task", "project", "global"],
                        "required": True,
                    },
                    "key": {
                        "type": "string",
                        "description": "The memory key name",
                        "required": True,
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to store",
                        "required": True,
                    },
                    "content_type": {
                        "type": "string",
                        "description": "Content format (default 'text')",
                        "enum": ["text", "structured", "code"],
                    },
                    "importance": {
                        "type": "number",
                        "description": (
                            "Importance score 0.0–1.0 (default 0.5). "
                            "Higher = retained longer."
                        ),
                    },
                },
            ),
            make_tool_definition(
                name="edit_file",
                description=(
                    "Write content to a file atomically (temp-file-then-rename). "
                    "Use create=True for new files (fails if exists), "
                    "create=False (default) to overwrite existing files (fails if not found). "
                    "For targeted line edits, prefer apply_diff instead."
                ),
                parameters={
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file",
                        "required": True,
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write (UTF-8)",
                        "required": True,
                    },
                    "create": {
                        "type": "boolean",
                        "description": (
                            "True = create new file (error if exists). "
                            "False = overwrite existing (error if not found)."
                        ),
                    },
                },
            ),
            make_tool_definition(
                name="search_memory",
                description=(
                    "Semantic search over long-term memory. Returns relevant "
                    "memories ranked by similarity to the query. Use for "
                    "finding past decisions, context, or project knowledge."
                ),
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                        "required": True,
                    },
                    "scope_type": {
                        "type": "string",
                        "description": "Search scope",
                        "enum": ["project", "global", "all"],
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Project ID for project-scoped search",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results (default 10, max 50)",
                    },
                },
            ),
            make_tool_definition(
                name="read_file",
                description=(
                    "Read a file from the local filesystem. Returns the full "
                    "content (truncated at 100KB). Always read files before "
                    "modifying them to understand existing code."
                ),
                parameters={
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file",
                        "required": True,
                    },
                },
            ),
            make_tool_definition(
                name="list_files",
                description=(
                    "List files in a directory matching a glob pattern. "
                    "Automatically skips .git, __pycache__, node_modules, "
                    ".venv, and target directories."
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
            make_tool_definition(
                name="symbol_search",
                description=(
                    "Search for code symbols (functions, classes, methods, etc.) "
                    "in the codebase knowledge graph. Returns symbol name, kind, "
                    "file location, and signature. Faster and more structured "
                    "than text search for finding specific definitions."
                ),
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Symbol name or pattern to search for",
                        "required": True,
                    },
                    "kind": {
                        "type": "string",
                        "description": "Filter by symbol kind",
                        "enum": [
                            "function", "method", "class", "struct",
                            "enum", "trait", "variable", "import",
                        ],
                    },
                },
            ),
            make_tool_definition(
                name="find_callers",
                description=(
                    "Find all call sites of a function or method. Returns "
                    "every location where the symbol is invoked. Essential "
                    "for understanding usage before modifying code."
                ),
                parameters={
                    "symbol": {
                        "type": "string",
                        "description": "Name of the function or method to find callers for",
                        "required": True,
                    },
                },
            ),
            make_tool_definition(
                name="find_callees",
                description=(
                    "Find what a function or method calls (its dependencies). "
                    "Returns the functions/methods invoked by the given symbol. "
                    "Useful for understanding a function's dependency footprint."
                ),
                parameters={
                    "symbol": {
                        "type": "string",
                        "description": "Name of the function or method to find callees for",
                        "required": True,
                    },
                },
            ),
            make_tool_definition(
                name="impact_analysis",
                description=(
                    "Analyze the blast radius of changing a symbol. Traverses "
                    "the call graph to find all symbols that would be affected "
                    "by modifying the given symbol. Run this BEFORE making "
                    "changes to understand the scope of impact."
                ),
                parameters={
                    "symbol": {
                        "type": "string",
                        "description": "Name of the symbol to analyze impact for",
                        "required": True,
                    },
                },
            ),
            make_tool_definition(
                name="explore_code",
                description=(
                    "Explore code structure via natural language query. Returns "
                    "a Markdown summary of relevant symbols, their callers, "
                    "callees, and dependencies. Use for high-level architecture "
                    "questions like 'how does authentication work' or 'what "
                    "calls the search engine'."
                ),
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Natural language question about the codebase",
                        "required": True,
                    },
                },
            ),
            make_tool_definition(
                name="run_command",
                description=(
                    "Execute a shell command and return stdout/stderr. "
                    "Use for running tests, linters, builds, git operations. "
                    "Has a configurable timeout (default 120s) to prevent hangs. "
                    "Output is truncated at 50KB stdout / 10KB stderr."
                ),
                parameters={
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                        "required": True,
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 120)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (default: project root)",
                    },
                },
            ),
            make_tool_definition(
                name="apply_diff",
                description=(
                    "Apply a unified diff patch to a file. More precise than "
                    "edit_file (which overwrites the entire file) — use this "
                    "for targeted line-range edits. Supports context lines for "
                    "fuzzy matching. Fails if a hunk cannot be applied."
                ),
                parameters={
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to patch",
                        "required": True,
                    },
                    "diff": {
                        "type": "string",
                        "description": "Unified diff content to apply",
                        "required": True,
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
        """Gather context from completed dependency subtasks + past experience.

        Args:
            subtask: The subtask whose dependencies to check.

        Returns:
            Formatted context string from prior subtask results and experience.
        """
        parts = []

        # Gather results from completed dependencies
        if self.engine is not None and subtask.depends_on:
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

        # Recall relevant past experience from memory
        if self.engine is not None:
            try:
                results = self.engine.search_memory(
                    query=subtask.description,
                    scope_type="all",
                    project_id=None,
                    max_results=3,
                )
                experience_parts = []
                for r in results:
                    key = getattr(r, "key", "")
                    if key.startswith("experience_"):
                        content = getattr(r, "content", None) or getattr(r, "entry", None)
                        if content:
                            text = getattr(content, "text", None) or str(content)
                            experience_parts.append(text[:200])
                if experience_parts:
                    parts.append("## Past Experience\n" + "\n".join(experience_parts))
            except Exception:
                logger.debug("Experience recall failed for subtask", exc_info=True)

        # Add codegraph knowledge graph context
        if self._codegraph.is_available():
            try:
                codegraph_ctx = self._codegraph.explore(subtask.description, max_nodes=10)
                if codegraph_ctx:
                    parts.append(f"## Code Knowledge Graph\n{codegraph_ctx}")
            except Exception:
                logger.debug("Codegraph explore failed for subtask", exc_info=True)

        if parts:
            return "\n".join(parts)
        return "(no prior context)"

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

        Looks for edit_file tool calls in the log and collects them as
        FileChange objects with diff content.

        Args:
            tool_log: Log of tool calls and results.

        Returns:
            List of FileChange objects.
        """
        modified = []
        for entry in tool_log:
            tool_name = entry.get("tool_call", {}).get("name", "")
            tool_input = entry.get("tool_call", {}).get("input", {})

            if tool_name == "edit_file":
                file_path = tool_input.get("file_path", "")
                create = tool_input.get("create", False)
                content = tool_input.get("content", "")
                if file_path:
                    # Build diff from content vs original file
                    diff = self._compute_file_diff(file_path, content)
                    modified.append(
                        FileChange(
                            file_path=file_path,
                            change_type=ChangeType.CREATED if create else ChangeType.MODIFIED,
                            diff=diff,
                        )
                    )
            elif tool_name == "apply_diff":
                file_path = tool_input.get("file_path", "")
                diff_content = tool_input.get("diff", "")
                if file_path:
                    modified.append(
                        FileChange(
                            file_path=file_path,
                            change_type=ChangeType.MODIFIED,
                            diff=diff_content,
                        )
                    )

        return modified

    @staticmethod
    def _compute_file_diff(file_path: str, new_content: str) -> str:
        """Compute a unified diff between the current file and new content."""
        import difflib
        try:
            with open(file_path) as f:
                old_lines = f.readlines()
        except FileNotFoundError:
            old_lines = []
        new_lines = new_content.splitlines(keepends=True)
        diff_lines = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{file_path}", tofile=f"b/{file_path}",
            lineterm="",
        )
        return "\n".join(diff_lines)

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


# ── Unified diff application ─────────────────────────────────────


def _apply_unified_diff(
    original: str,
    diff_text: str,
) -> tuple[str, int, list[str]]:
    """Apply a unified diff to original text.

    Returns (patched_text, hunks_applied, errors).
    If any hunk fails, returns errors for that hunk without modifying.

    ponytail: minimal patch algorithm — handles the common case of
    sequential hunks with context lines. Falls back to fuzzy matching
    (±2 line offset) when exact match fails.
    """
    lines = original.splitlines(True)  # keep line endings
    hunks = _parse_unified_hunks(diff_text)

    hunks_applied = 0
    errors: list[str] = []

    # Apply hunks in reverse order so line offsets don't shift
    for hunk in reversed(hunks):
        old_start = hunk["old_start"]  # 1-based
        old_lines = hunk["old_lines"]
        new_lines = hunk["new_lines"]

        # Try exact match first, then fuzzy ±3 lines
        match_offset = _find_hunk_match(lines, old_start, old_lines, fuzzy=3)
        if match_offset is None:
            errors.append(
                f"Hunk at line {old_start} (context: {old_lines[0][:60]}...) "
                "did not match"
            )
            continue

        # Replace lines at match_offset
        lines[match_offset : match_offset + len(old_lines)] = new_lines
        hunks_applied += 1

    return "".join(lines), hunks_applied, errors


_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
)


def _parse_unified_hunks(diff_text: str) -> list[dict[str, Any]]:
    """Parse unified diff into a list of hunks."""
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            old_start = int(m.group(1))
            current = {
                "old_start": old_start,
                "old_lines": [],
                "new_lines": [],
            }
            continue

        if current is None:
            continue

        if line.startswith("+"):
            current["new_lines"].append(line[1:] + "\n")
        elif line.startswith("-"):
            current["old_lines"].append(line[1:] + "\n")
        elif line.startswith(" "):
            current["old_lines"].append(line[1:] + "\n")
            current["new_lines"].append(line[1:] + "\n")

    if current is not None:
        hunks.append(current)

    return hunks


def _find_hunk_match(
    lines: list[str],
    old_start: int,
    old_lines: list[str],
    fuzzy: int = 3,
) -> int | None:
    """Find where old_lines matches in lines, starting at old_start-1.

    Returns the 0-based index where the match starts, or None.
    Tries exact position first, then fuzzy ±offset up to `fuzzy` lines.
    """
    target_len = len(old_lines)
    if target_len == 0:
        return old_start - 1

    def _matches(at: int) -> bool:
        if at < 0 or at + target_len > len(lines):
            return False
        for i, ol in enumerate(old_lines):
            if lines[at + i].rstrip("\r\n") != ol.rstrip("\r\n"):
                return False
        return True

    # Exact match
    idx = old_start - 1
    if _matches(idx):
        return idx

    # Fuzzy: try offsets 1..fuzzy both directions
    for offset in range(1, fuzzy + 1):
        if _matches(idx - offset):
            return idx - offset
        if _matches(idx + offset):
            return idx + offset

    return None
