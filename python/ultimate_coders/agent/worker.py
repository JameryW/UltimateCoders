"""Worker — executes subtasks via sandbox agent (Claude Code / Codex).

Previously supported an LLM tool-calling mode — removed in favor of
sandbox-only execution. Coding agents (Claude Code, Codex) have their
own tool chains and don't need a Python-side tool-calling loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
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
from ultimate_coders.agent.state_sync import (
    ContextInjector,
    FileChangeEvent,
    FileChangeEventType,
)
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    WorkerInfo,
)
from ultimate_coders.agent.workspace import WorkspaceManager

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
# Claude Code stream-json events: {"type":"tool_use","name":"...","input":{...}}
_STREAM_JSON_TOOL_USE_RE = re.compile(r'^\s*\{.*"type"\s*:\s*"tool_use"')
_STREAM_JSON_TOOL_RESULT_RE = re.compile(r'^\s*\{.*"type"\s*:\s*"tool_result"')


def _parse_sandbox_line(line: str) -> tuple[str, dict[str, Any]] | None:
    """Parse a single sandbox stdout line into an event type + data.

    Returns None for uninteresting lines (plain text output, blank lines).
    Recognized patterns:
    - Stream-JSON events: {"type":"tool_use",...} / {"type":"tool_result",...}
    - Tool calls: "⚙ ToolName(args)" → ("tool_call", {...})
    - File changes: "📝 path" → ("file_modified", {...})
    - Diff blocks: "```diff" → ("diff_start", {})
    - Thinking: "💭 ..." → ("thinking", {...})

    ponytail: try JSON first (stream-json mode), then regex fallback.
    """
    if not line.strip():
        return None

    # ── Try stream-JSON parsing first ────────────────────────
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            evt_type = obj.get("type", "")
            if evt_type == "tool_use":
                return ("tool_call", {
                    "tool_name": obj.get("name", "unknown"),
                    "tool_id": obj.get("id", ""),
                    "input_summary": json.dumps(obj.get("input", {}), ensure_ascii=False)[:300],
                })
            if evt_type == "tool_result":
                content = obj.get("content", "")
                if isinstance(content, list):
                    # content_block list — extract text summaries
                    texts = []
                    for block in content:
                        if isinstance(block, dict):
                            texts.append(block.get("text", "")[:100])
                    content = " ".join(texts)[:300]
                elif isinstance(content, str):
                    content = content[:300]
                else:
                    content = str(content)[:300]
                return ("tool_result", {
                    "tool_id": obj.get("tool_use_id", ""),
                    "is_error": obj.get("is_error", False),
                    "content_summary": content,
                })
            # Other stream-json event types (assistant, result, etc.) — skip
            return None
        except (json.JSONDecodeError, TypeError):
            pass  # Not valid JSON — fall through to regex

    # ── Regex fallback ────────────────────────────────────────

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
        workspace_manager: WorkspaceManager | None = None,
    ):
        self.worker_id = worker_id or str(uuid.uuid4())
        self.engine = engine
        self._sandbox_config = sandbox_config or SandboxConfig()
        self.capabilities = capabilities or self._derive_capabilities()
        self.max_capacity = max_capacity
        self.current_task: Subtask | None = None
        self._active_count = 0
        self.conflict_detector = conflict_detector or ConflictDetector()

        # Sandbox execution (always)
        self._sandbox_manager = SandboxManager(self._sandbox_config, engine)

        # Event publishing — NATS (preferred) or local event_emitter fallback
        self.nats_publisher = nats_publisher
        self.event_emitter = event_emitter

        # Workspace isolation for distributed execution
        self._workspace_manager = workspace_manager

        # Context injection — completed subtask summaries flow to dependents
        self._context_injector = ContextInjector()

        # Self-heartbeat monitoring — track last heartbeat timestamp
        # so stale_worker_cleanup can detect local worker stalls
        self._last_heartbeat_at: datetime = datetime.now(timezone.utc)

    def _derive_capabilities(self) -> list[str]:
        """Derive worker capabilities from SandboxConfig tool/mcp settings.

        ponytail: simple string matching — upgrade path is tool introspection.
        """
        caps = ["code", "search", "memory", "test", "decompose", "review"]
        cfg = self._sandbox_config
        if cfg.mcp_configs:
            caps.append("mcp")
            # Derive per-server capabilities from mcp_configs
            for entry in (cfg.mcp_configs or []):
                if isinstance(entry, dict):
                    for name in entry:
                        caps.append(f"mcp:{name}")
                elif isinstance(entry, str) and (os.sep in entry or "/" in entry):
                    # File path — extract server name from filename
                    name = os.path.basename(entry.replace("/", os.sep)).replace(".json", "")
                    caps.append(f"mcp:{name}")
        if cfg.tools:
            for t in cfg.tools:
                if t.startswith("mcp__"):
                    # mcp__<server>__* → mcp:<server>
                    parts = t.split("__")
                    if len(parts) >= 2:
                        caps.append(f"mcp:{parts[1]}")
                if t.startswith("mcp__codegraph") and "codegraph" not in caps:
                    caps.append("codegraph")
        if cfg.agent_name:
            caps.append(f"agent:{cfg.agent_name}")
        if cfg.agents_json:
            # Parse agent names from agents_json
            try:
                agents = (
                    json.loads(cfg.agents_json)
                    if isinstance(cfg.agents_json, str)
                    else cfg.agents_json
                )
                for name in (agents if isinstance(agents, dict) else []):
                    caps.append(f"agent:{name}")
            except (json.JSONDecodeError, TypeError):
                pass
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for c in caps:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique

    # ── Agent Config Profiles & Templates ───────────────────────

    AGENT_PROFILES: dict[str, dict[str, Any]] = {
        "review": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "append_system_prompt": (
                "Read-only review mode — analyze and report only,"
                " do not modify files."
            ),
        },
        "codegraph": {
            "tools": ["default", "mcp__codegraph__*"],
        },
        "code": {
            "tools": ["default"],
        },
        "test": {
            "tools": ["default"],
            "append_system_prompt": (
                "Focus on writing and running tests. Verify existing"
                " behavior, add coverage for edge cases."
            ),
        },
        "fix": {
            "tools": ["default"],
            "append_system_prompt": (
                "Fix the bug with the minimal diff. Do not refactor"
                " unrelated code."
            ),
        },
        "refactor": {
            "tools": ["default", "mcp__codegraph__*"],
            "append_system_prompt": (
                "Refactor preserving existing behavior. Use codegraph"
                " to understand callers before changing APIs."
            ),
        },
        "deploy": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "append_system_prompt": (
                "Deploy/check mode — run deployment commands and"
                " verify status only, do not modify source files."
            ),
        },
        "docs": {
            "tools": ["default"],
            "append_system_prompt": (
                "Focus on writing documentation. Update README,"
                " docstrings, and inline comments."
            ),
        },
    }

    SUBTASK_TEMPLATES: dict[str, dict[str, Any]] = {
        "review": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "append_system_prompt": (
                "Read-only review mode — analyze and report only,"
                " do not modify files."
            ),
        },
        "search": {
            "tools": ["default", "mcp__codegraph__*"],
        },
        "test": {
            "append_system_prompt": (
                "Write and run tests for the described behavior."
            ),
        },
        "fix": {
            "append_system_prompt": (
                "Fix the described bug with minimal changes."
            ),
        },
        "refactor": {
            "tools": ["default", "mcp__codegraph__*"],
            "append_system_prompt": (
                "Refactor the described code preserving behavior."
            ),
        },
        "deploy": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "append_system_prompt": "Deploy/check mode only.",
        },
        "docs": {
            "append_system_prompt": "Write documentation for the described topic.",
        },
    }

    def _resolve_agent_config(self, subtask: Subtask) -> dict[str, Any]:
        """Derive agent_config for a subtask: explicit > capability match > type template.

        Priority:
        1. subtask.agent_config — explicit user override, use as-is
        2. required_capabilities — match against AGENT_PROFILES
        3. subtask description heuristics — match against SUBTASK_TEMPLATES

        ponytail: simple dict merge — upgrade path is a proper profile resolution system.
        """
        # 1. Explicit override
        if subtask.agent_config:
            return subtask.agent_config

        config: dict[str, Any] = {}

        # 2. Capability matching (takes precedence)
        for cap in subtask.required_capabilities:
            if cap in self.AGENT_PROFILES:
                config.update(self.AGENT_PROFILES[cap])

        # 3. Type template (applied after, but doesn't override capability matches)
        template = self._match_subtask_template(subtask)
        if template:
            for k, v in template.items():
                if k not in config:
                    config[k] = v

        return config

    @staticmethod
    def _match_subtask_template(subtask: Subtask) -> dict[str, Any] | None:
        """Match a subtask to a template by description heuristics.

        ponytail: keyword matching — upgrade path is LLM classification.
        """
        desc_lower = subtask.description.lower()
        if any(kw in desc_lower for kw in ("review", "audit", "analyze", "inspect")):
            return Worker.SUBTASK_TEMPLATES.get("review")
        if any(kw in desc_lower for kw in ("search", "find", "locate", "grep")):
            return Worker.SUBTASK_TEMPLATES.get("search")
        if any(kw in desc_lower for kw in ("test", "spec", "coverage", "unit test")):
            return Worker.SUBTASK_TEMPLATES.get("test")
        if any(kw in desc_lower for kw in ("fix", "bug", "patch", "repair", "resolve error")):
            return Worker.SUBTASK_TEMPLATES.get("fix")
        if any(kw in desc_lower for kw in ("refactor", "restructure", "reorganize", "clean up")):
            return Worker.SUBTASK_TEMPLATES.get("refactor")
        if any(kw in desc_lower for kw in ("deploy", "release", "publish", "ship")):
            return Worker.SUBTASK_TEMPLATES.get("deploy")
        if any(kw in desc_lower for kw in ("document", "docs", "readme", "docstring", "comment")):
            return Worker.SUBTASK_TEMPLATES.get("docs")
        return None

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

    MAX_RETRIES: int = 3
    RETRY_DELAYS: list[float] = [2.0, 4.0]  # delays before retry 1, 2

    async def execute_subtask(self, subtask: Subtask) -> SubtaskResult:
        """Execute a subtask via sandbox agent.

        Includes workspace isolation, context injection, checkpoint support,
        and automatic retry with backoff on failure:
        - Acquires a workspace (git worktree) for file-modifying subtasks
        - Injects completed dependency context into the subtask prompt
        - Saves intermediate results as checkpoints for resume
        - Broadcasts file change events for distributed state sync
        - Retries up to MAX_RETRIES times with backoff on failure
        """
        self.current_task = subtask
        self._active_count += 1
        subtask.status = SubtaskStatus.IN_PROGRESS

        try:
            # Check for existing checkpoint — skip if already completed
            checkpoint = self._load_checkpoint(subtask.id)
            if checkpoint is not None and checkpoint.get("success"):
                logger.info(
                    "Subtask %s has completed checkpoint, skipping execution",
                    subtask.id[:8],
                )
                # Restore full SubtaskResult from checkpoint
                from ultimate_coders.agent.types import ChangeType, FileChange
                mod_files = [
                    FileChange(
                        file_path=f.get("file_path", ""),
                        change_type=ChangeType(f.get("change_type", "modified")),
                    )
                    for f in (checkpoint.get("modified_files") or [])
                ]
                return SubtaskResult(
                    subtask_id=subtask.id,
                    worker_id=checkpoint.get("worker_id", self.worker_id),
                    summary=checkpoint.get("summary", "Resumed from checkpoint"),
                    success=True,
                    modified_files=mod_files,
                    recent_tool_calls=checkpoint.get("tool_calls", []),
                    stderr_tail=checkpoint.get("stderr_tail", ""),
                )

            # Inject context from completed dependencies
            context_block = self._context_injector.build_context(subtask.depends_on)

            # Auto-inject cross-repo search context (when engine is available)
            search_block = self._build_search_context(subtask)
            if search_block:
                if context_block:
                    context_block = f"{context_block}\n\n{search_block}"
                else:
                    context_block = search_block

            # Acquire workspace if this subtask modifies files
            workspace_handle = None
            if self._workspace_manager and subtask.file_constraints:
                workspace_handle = await self._workspace_manager.acquire(subtask.id)
                if workspace_handle:
                    logger.info(
                        "Subtask %s allocated workspace %s",
                        subtask.id[:8], workspace_handle.workspace_id,
                    )

            await self._publish_event(
                "subtask_started",
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                data={"description": subtask.description, "worker_id": self.worker_id},
            )

            # ── Progress helper ─────────────────────────────────────────
            async def _progress(phase: str, percent: int, **extra: Any) -> None:
                await self._publish_event(
                    "subtask_progress",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data={"phase": phase, "percent": percent, "worker_id": self.worker_id, **extra},
                )

            await _progress("preparing", 10)

            # ── Retry loop with backoff ────────────────────────────────
            result: SubtaskResult | None = None
            for attempt in range(self.MAX_RETRIES):
                try:
                    timeout_secs = subtask.timeout_seconds or 600
                    try:
                        await _progress("executing", 50)
                        result = await asyncio.wait_for(
                            self._execute_in_sandbox(subtask, context_block, workspace_handle),
                            timeout=timeout_secs,
                        )
                        await _progress("validating", 80)
                    except asyncio.TimeoutError:
                        result = SubtaskResult(
                            subtask_id=subtask.id,
                            worker_id=self.worker_id,
                            summary=f"Subtask timed out after {timeout_secs}s",
                            success=False,
                        )

                    # Save checkpoint for resume
                    self._save_checkpoint(subtask.id, result)

                    # Record result in context injector for dependent subtasks
                    self._context_injector.add_result(
                        subtask_id=subtask.id,
                        summary=result.summary,
                        modified_files=[f.file_path for f in (result.modified_files or [])],
                        success=result.success,
                    )

                    # Broadcast file change events for distributed state sync
                    if result.success and result.modified_files:
                        await self._broadcast_file_changes(subtask, result)

                    # Release workspace (merge if successful)
                    if workspace_handle:
                        merge_result = await self._workspace_manager.release(
                            workspace_handle, merge=result.success,
                        )
                        if merge_result.get("status") == "conflict":
                            logger.warning(
                                "Workspace merge conflict for subtask %s, branch preserved: %s",
                                subtask.id[:8], merge_result.get("branch_preserved", ""),
                            )

                    if result.success:
                        await _progress("finalizing", 95)
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
                        failure_data: dict[str, Any] = {
                            "error": result.summary[:300],
                            "worker_id": self.worker_id,
                        }
                        if result.stderr_tail:
                            failure_data["stderr_tail"] = result.stderr_tail
                        if result.recent_tool_calls:
                            failure_data["recent_tools"] = json.dumps(result.recent_tool_calls)

                        # Retry if attempts remain
                        if attempt < self.MAX_RETRIES - 1:
                            subtask.retry_count += 1
                            result.retry_count = subtask.retry_count
                            delay = (
                                self.RETRY_DELAYS[attempt]
                                if attempt < len(self.RETRY_DELAYS)
                                else self.RETRY_DELAYS[-1]
                            )
                            failure_data["retry"] = True
                            failure_data["retry_attempt"] = attempt + 1
                            failure_data["retry_delay"] = delay
                            await self._publish_event(
                                "subtask_failed",
                                task_id=subtask.parent_id,
                                subtask_id=subtask.id,
                                data=failure_data,
                            )
                            await self._publish_event(
                                "subtask_retry",
                                task_id=subtask.parent_id,
                                subtask_id=subtask.id,
                                data={
                                    "attempt": attempt + 1,
                                    "max_retries": self.MAX_RETRIES,
                                    "delay": delay,
                                    "worker_id": self.worker_id,
                                },
                            )
                            logger.info(
                                "Retrying subtask %s (attempt %d/%d, delay %.1fs)",
                                subtask.id[:8], attempt + 1, self.MAX_RETRIES, delay,
                            )
                            await asyncio.sleep(delay)
                            continue  # retry loop

                        # Final failure — no more retries
                        await self._publish_event(
                            "subtask_failed",
                            task_id=subtask.parent_id,
                            subtask_id=subtask.id,
                            data=failure_data,
                        )

                    result.retry_count = subtask.retry_count
                    return result

                except Exception as e:
                    logger.error("Subtask %s execution failed: %s", subtask.id, e, exc_info=True)
                    # Release workspace without merge on error
                    if workspace_handle:
                        await self._workspace_manager.release(workspace_handle, merge=False)

                    # Retry on exception too, if attempts remain
                    if attempt < self.MAX_RETRIES - 1:
                        subtask.retry_count += 1
                        delay = (
                            self.RETRY_DELAYS[attempt]
                            if attempt < len(self.RETRY_DELAYS)
                            else self.RETRY_DELAYS[-1]
                        )
                        await self._publish_event(
                            "subtask_retry",
                            task_id=subtask.parent_id,
                            subtask_id=subtask.id,
                            data={
                                "attempt": attempt + 1,
                                "max_retries": self.MAX_RETRIES,
                                "delay": delay,
                                "worker_id": self.worker_id,
                                "error": str(e)[:200],
                            },
                        )
                        logger.info(
                            "Retrying subtask %s after exception (attempt %d/%d)",
                            subtask.id[:8], attempt + 1, self.MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue

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
                        retry_count=subtask.retry_count,
                    )

            # Should not reach here, but safety net
            # ponytail: unreachable guard — all branches return or continue
            return result or SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary="All retry attempts exhausted",
                success=False,
                retry_count=subtask.retry_count,
            )

        finally:
            self.current_task = None
            self._active_count = max(0, self._active_count - 1)

    def _save_checkpoint(self, subtask_id: str, result: SubtaskResult) -> None:
        """Persist subtask result to engine memory for checkpoint/resume.

        Stores full result including modified_files, tool_calls, and error
        so that resume can reconstruct a complete SubtaskResult.

        ponytail: synchronous write — engine.write_memory may be async,
        but we use in-memory fallback so it's fine. Non-fatal on failure.
        """
        if self.engine is None:
            return
        try:
            data: dict[str, Any] = {
                "subtask_id": subtask_id,
                "worker_id": result.worker_id,
                "summary": result.summary,
                "success": result.success,
                "modified_files": [
                    {"file_path": f.file_path, "change_type": f.change_type.value}
                    for f in (result.modified_files or [])
                ],
                "tool_calls": result.recent_tool_calls[-5:] if result.recent_tool_calls else [],
                "error": result.summary if not result.success else None,
                "stderr_tail": result.stderr_tail,
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

    async def _execute_in_sandbox(
        self,
        subtask: Subtask,
        context_block: str = "",
        workspace_handle: Any | None = None,
    ) -> SubtaskResult:
        """Execute via sandbox (Claude Code / Codex).

        Automatically declares and releases EditIntent for file_constraints
        so the conflict detector tracks which files are being modified.

        If context_block is provided, prepends it to the subtask prompt.
        If workspace_handle is provided, executes in the workspace directory.
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
            # Build prompt with context injection
            description = subtask.description
            if context_block:
                description = f"{context_block}\n\n{description}"

            prompt = _SUBTASK_USER_TEMPLATE.format(
                description=description,
                expected_output=subtask.expected_output or "Complete the described task",
                file_constraints=", ".join(subtask.file_constraints) or "none",
            )

            # Determine working directory (workspace isolation)
            working_dir: str | None = None
            if workspace_handle and hasattr(workspace_handle, "worktree_path"):
                working_dir = workspace_handle.worktree_path or None

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
                prompt,
                working_dir=working_dir,
                on_stdout_line=_on_stdout_line,
                subtask_config=self._resolve_agent_config(subtask) or None,
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

    async def _broadcast_file_changes(
        self, subtask: Subtask, result: SubtaskResult,
    ) -> None:
        """Broadcast file change events via NATS for distributed state sync.

        Each modified file gets a FileChangeEvent published to
        ``uc.file.changed`` so all workers and the orchestrator
        can track real-time file modifications.
        """
        if not result.modified_files or not self.nats_publisher:
            return

        for fc in result.modified_files:
            event = FileChangeEvent(
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                file_path=fc.file_path,
                change_type=FileChangeEventType(fc.change_type.value),
                diff_summary=fc.diff[:200] if fc.diff else "",
            )
            try:
                await self.nats_publisher.publish_event(
                    "file_changed",
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                    data=event.to_dict(),
                )
            except Exception:
                logger.debug("Failed to broadcast file change for %s", fc.file_path)

    async def send_heartbeat(self) -> dict[str, Any]:
        self._last_heartbeat_at = datetime.now(timezone.utc)
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

    def _build_search_context(self, subtask: Subtask) -> str | None:
        """Search across repos for code relevant to the subtask description.

        Returns a formatted context block with search results, or None.
        """
        if self.engine is None or not subtask.description:
            return None
        try:
            from ultimate_coders.search.query import SearchQuery
            sq = SearchQuery(subtask.description).with_modes(["hybrid"]).limit(10)
            if subtask.project_id:
                sq.in_repos([subtask.project_id])
            else:
                sq.in_all_repos(self.engine)
            result = self.engine.search(sq)
            items = getattr(result, "items", result) if result else []
            if not items:
                return None
            lines = ["## Related code from indexed repositories"]
            for r in items[:8]:
                repo = getattr(r, "repo_id", "?")
                path = getattr(r, "file_path", "?")
                snippet = getattr(r, "content_snippet", "")
                if snippet:
                    lines.append(f"### [{repo}] {path}")
                    lines.append(f"```\n{snippet[:500]}\n```")
            return "\n".join(lines) if len(lines) > 1 else None
        except Exception:
            # ponytail: search failure is non-fatal — subtask still executes
            return None

    def search_across_repos(
        self, query: str, modes: list[str] | None = None, max_results: int = 20,
    ) -> list | None:
        """Search across all indexed repositories via the Engine.

        Routes through gRPC Gateway when configured, enabling cross-repo
        retrieval from the shared search index.

        Args:
            query: Search text (natural language or code pattern).
            modes: Search modes — default ["hybrid"].
            max_results: Max results to return.

        Returns:
            SearchResult items, or None if engine is unavailable.
        """
        if self.engine is None:
            return None
        from ultimate_coders.search.query import SearchQuery
        sq = SearchQuery(query).in_all_repos(self.engine)
        if modes:
            sq.with_modes(modes)
        sq.limit(max_results)
        result = self.engine.search(sq)
        return getattr(result, "items", result) if result else None

    def read_shared_memory(
        self, key: str, project_id: str = "",
    ) -> object | None:
        """Read project-scoped memory (shared across Workers via Gateway).

        Args:
            key: Memory key name.
            project_id: Project scope (uses subtask's project_id if empty).

        Returns:
            MemoryEntry or None.
        """
        if self.engine is None:
            return None
        pid = project_id or getattr(self.current_task, "project_id", "")
        scope = "project" if pid else "global"
        return self.engine.read_memory(
            key_scope=scope, key=key, project_id=pid or None,
        )

    def write_shared_memory(
        self, key: str, content: str, project_id: str = "",
        content_type: str = "text", importance: float = 0.7,
        tags: list[str] | None = None,
    ) -> object | None:
        """Write project-scoped memory (shared across Workers via Gateway).

        Args:
            key: Memory key name.
            content: Content to store.
            project_id: Project scope (uses subtask's project_id if empty).
            content_type: "text", "structured", "code", "diff", or "reference".
            importance: Importance score (default 0.7 — above long-term threshold).
            tags: Tags for categorization.

        Returns:
            MemoryEntry or None.
        """
        if self.engine is None:
            return None
        pid = project_id or getattr(self.current_task, "project_id", "")
        scope = "project" if pid else "global"
        return self.engine.write_memory(
            key_scope=scope, key=key, content=content,
            content_type=content_type, source_agent=f"worker:{self.worker_id}",
            importance=importance, tags=tags, project_id=pid or None,
        )
