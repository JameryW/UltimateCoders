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
import shutil
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
from ultimate_coders.agent.llm import (
    LLMRetryExhaustedError,
    _classify_llm_error,
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
    FileChange,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    WorkerInfo,
)
from ultimate_coders.agent.workspace import WorkspaceHandle, WorkspaceManager

logger = logging.getLogger(__name__)

# ponytail: map mcp:<server> tags to semantic capability aliases.
# Add rows as more in-process MCP servers land.
_MCP_CAP_ALIASES: dict[str, str] = {
    "mcp:uc-fs": "file-edit",
    "mcp:uc-engine": "search",  # uc-engine already implies search; no-op if present
    "mcp:uc-lsp": "lsp",  # uc-lsp provides real-time LSP symbol tools
}

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
                return (
                    "tool_call",
                    {
                        "tool_name": obj.get("name", "unknown"),
                        "tool_id": obj.get("id", ""),
                        "input_summary": json.dumps(obj.get("input", {}), ensure_ascii=False)[:300],
                    },
                )
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
                return (
                    "tool_result",
                    {
                        "tool_id": obj.get("tool_use_id", ""),
                        "is_error": obj.get("is_error", False),
                        "content_summary": content,
                    },
                )
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
        return (
            "tool_call",
            {
                "tool_name": tool_name,
                "args": tool_args[:200],
            },
        )

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


def _build_friendly_error(e: Exception) -> tuple[str, str]:
    """Build a friendly error summary and error field from an exception.

    Detects LLMRetryExhaustedError to extract the classification (transient/
    permanent, retry count). Falls back to generic classification for other
    exceptions.

    Returns:
        (summary, error) tuple — summary is the user-facing friendly message,
        error is the root-cause string for the SubtaskResult.error field.
    """
    if isinstance(e, LLMRetryExhaustedError):
        cls = e.classification
        root = cls.message.strip()
        if cls.kind == "transient":
            summary = f"LLM 瞬时错误（已重试 {cls.retry_count} 次）: {root[:200]}"
        elif cls.kind == "permanent":
            summary = f"LLM 永久错误: {root[:200]}"
        else:
            summary = f"LLM 错误（已重试 {cls.retry_count} 次）: {root[:200]}"
        return summary, root[:2000]

    # Non-LLM exception — try classification anyway (string-based, no harm)
    cls = _classify_llm_error(e, 0)
    root = str(e).strip()
    if cls.kind == "transient":
        return f"瞬时错误: {root[:200]}", root[:2000]
    if cls.kind == "permanent":
        return f"永久错误: {root[:200]}", root[:2000]
    return f"Execution error: {root[:200]}", root[:2000]


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
        self.worker_id = worker_id or os.environ.get("UC_WORKER_ID") or str(uuid.uuid4())
        self.engine = engine
        self._sandbox_config = sandbox_config or SandboxConfig()
        # Auto-register in-process MCP servers for sandbox agent tools.
        # uc-engine: search + memory (needs engine). uc-fs: file edit (workspace-scoped).
        # Guarded on engine != None so test stubs (SandboxConfig() + no engine) stay
        # MCP-free, preserving the legacy "no mcp_configs ⇒ no mcp capability" contract.
        if engine is not None and self._sandbox_config.mcp_configs is None:
            self._sandbox_config.mcp_configs = [
                {
                    "uc-engine": {
                        "command": "python",
                        "args": ["-m", "ultimate_coders.agent.engine_mcp"],
                    },
                },
                {
                    "uc-fs": {
                        "command": "python",
                        "args": ["-m", "ultimate_coders.agent.fs_mcp"],
                    },
                },
                {
                    "uc-lsp": {
                        "command": "python",
                        "args": ["-m", "ultimate_coders.agent.lsp_mcp"],
                    },
                },
            ]
        elif engine is not None and self._sandbox_config.mcp_configs is not None:
            # ponytail: ensure uc-fs + uc-lsp are registered even if caller
            # supplied custom mcp_configs
            names = {
                name
                for entry in self._sandbox_config.mcp_configs or []
                if isinstance(entry, dict)
                for name in entry
            }
            if "uc-fs" not in names:
                self._sandbox_config.mcp_configs.append(
                    {
                        "uc-fs": {
                            "command": "python",
                            "args": ["-m", "ultimate_coders.agent.fs_mcp"],
                        },
                    }
                )
            if "uc-lsp" not in names:
                self._sandbox_config.mcp_configs.append(
                    {
                        "uc-lsp": {
                            "command": "python",
                            "args": ["-m", "ultimate_coders.agent.lsp_mcp"],
                        },
                    }
                )
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

        # Search cache — LRU + TTL to reduce gRPC round-trips
        from ultimate_coders.agent.search_cache import get_default_cache

        self._search_cache = get_default_cache()

        # Strong refs for fire-and-forget broadcast tasks. asyncio only holds
        # tasks weakly, so an unreferenced create_task() can be GC'd before it
        # completes (CPython documented behavior). done callback self-removes.
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def _derive_capabilities(self) -> list[str]:
        """Derive worker capabilities from SandboxConfig tool/mcp settings.

        ponytail: simple string matching — upgrade path is tool introspection.
        """
        caps = ["code", "search", "memory", "test", "decompose", "review"]
        cfg = self._sandbox_config
        if cfg.mcp_configs:
            caps.append("mcp")
            # Derive per-server capabilities from mcp_configs
            for entry in cfg.mcp_configs or []:
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
                    # codegraph MCP provides LSP semantics (goToDefinition/findReferences/hover/
                    # documentSymbol) — declare lsp so the scheduler routes symbol-ops subtasks.
                    # ponytail: codegraph lags writes ~1s; uc-lsp (multilspy) now provides
                    # real-time LSP for Python. Both coexist — codegraph for cross-repo/history.
                    caps.append("lsp")
        # Map specific mcp:<server> tags to semantic capability aliases.
        for entry in cfg.mcp_configs or []:
            server_names: list[str] = []
            if isinstance(entry, dict):
                server_names = list(entry.keys())
            elif isinstance(entry, str) and ("/" in entry or os.sep in entry):
                server_names = [os.path.basename(entry.replace("/", os.sep)).replace(".json", "")]
            for sn in server_names:
                tag = f"mcp:{sn}"
                alias = _MCP_CAP_ALIASES.get(tag)
                if alias and alias not in caps:
                    caps.append(alias)
        # Opt-in capability flags for tools whose server bodies aren't implemented here.
        # ponytail: declare-only extension point — set UC_CAP_BROWSER/UC_CAP_DEBUG to advertise
        # the capability when an external MCP server provides it.
        for flag, cap in (("UC_CAP_BROWSER", "browser"), ("UC_CAP_DEBUG", "debug")):
            if os.environ.get(flag):
                caps.append(cap)
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
                for name in agents if isinstance(agents, dict) else []:
                    caps.append(f"agent:{name}")
            except (json.JSONDecodeError, TypeError):
                pass
        # ponytail: probe agent CLIs on PATH — advertise matching capabilities so
        # the scheduler can route codex/claude-code step subtasks to workers that
        # actually have the CLI. Best-effort: missing CLI = capability not advertised.
        if shutil.which("claude"):
            caps.append("claude-code")
        if shutil.which("codex"):
            caps.append("codex")
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
                "Read-only review mode — analyze and report only, do not modify files."
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
                "Fix the bug with the minimal diff. Do not refactor unrelated code."
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
                "Focus on writing documentation. Update README, docstrings, and inline comments."
            ),
        },
        # Explicit file-edit profile: opt into the in-process uc-fs MCP for precise
        # str_replace / write_file ops. Distinct from the default code profile which
        # relies on the spawned agent's built-in Edit/Write tools.
        "file-edit": {
            "tools": ["default", "mcp__uc-fs__*"],
            "append_system_prompt": (
                "Precise file editing mode — use uc-fs tools"
                " (read_file/write_file/edit_file) for surgical edits."
            ),
        },
    }

    SUBTASK_TEMPLATES: dict[str, dict[str, Any]] = {
        "review": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "append_system_prompt": (
                "Read-only review mode — analyze and report only, do not modify files."
            ),
        },
        "search": {
            "tools": ["default", "mcp__codegraph__*"],
        },
        "test": {
            "append_system_prompt": ("Write and run tests for the described behavior."),
        },
        "fix": {
            "append_system_prompt": ("Fix the described bug with minimal changes."),
        },
        "refactor": {
            "tools": ["default", "mcp__codegraph__*"],
            "append_system_prompt": ("Refactor the described code preserving behavior."),
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
                event_type,
                task_id=task_id,
                subtask_id=subtask_id,
                data=data,
            )
        elif self.event_emitter is not None:
            await self.event_emitter.emit(
                event_type,
                task_id=task_id,
                subtask_id=subtask_id,
                data=data,
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
                        subtask.id[:8],
                        workspace_handle.workspace_id,
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
                # Re-acquire workspace each attempt: prior attempt released it
                # (or never had one). Without this, retry runs against a freed
                # worktree and double-releases. ponytail: per-attempt acquire
                if (
                    self._workspace_manager
                    and subtask.file_constraints
                    and workspace_handle is None
                ):
                    workspace_handle = await self._workspace_manager.acquire(subtask.id)
                    if workspace_handle:
                        logger.info(
                            "Subtask %s re-acquired workspace %s (attempt %d)",
                            subtask.id[:8],
                            workspace_handle.workspace_id,
                            attempt + 1,
                        )
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
                            error=f"Subtask timed out after {timeout_secs}s",
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
                        await self._broadcast_file_changes(subtask, result, workspace_handle)

                    # Release workspace (merge if successful)
                    if workspace_handle:
                        merge_result = await self._workspace_manager.release(
                            workspace_handle,
                            merge=result.success,
                        )
                        if merge_result.get("status") == "conflict":
                            logger.warning(
                                "Workspace merge conflict for subtask %s, branch preserved: %s",
                                subtask.id[:8],
                                merge_result.get("branch_preserved", ""),
                            )
                        workspace_handle = None  # ponytail: freed; re-acquire next attempt

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
                                subtask.id[:8],
                                attempt + 1,
                                self.MAX_RETRIES,
                                delay,
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
                        workspace_handle = None  # ponytail: freed; re-acquire next attempt

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
                            subtask.id[:8],
                            attempt + 1,
                            self.MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue

                    await self._publish_event(
                        "subtask_failed",
                        task_id=subtask.parent_id,
                        subtask_id=subtask.id,
                        data={"error": str(e), "worker_id": self.worker_id},
                    )
                    friendly_summary, error_field = _build_friendly_error(e)
                    return SubtaskResult(
                        subtask_id=subtask.id,
                        worker_id=self.worker_id,
                        summary=friendly_summary,
                        success=False,
                        error=error_field,
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
                        fp,
                        result.value,
                    )
                declared_files.append(fp)

        try:
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

            # Multi-agent workflow: run ordered steps, threading each step's
            # output into the next step's prompt template. Empty steps = the
            # legacy single-agent path (backward compatible).
            if subtask.steps:
                output = await self._execute_steps(
                    subtask, working_dir, _on_stdout_line, context_block
                )
            else:
                # Build prompt with context injection
                description = subtask.description
                if context_block:
                    description = f"{context_block}\n\n{description}"

                prompt = _SUBTASK_USER_TEMPLATE.format(
                    description=description,
                    expected_output=subtask.expected_output or "Complete the described task",
                    file_constraints=", ".join(subtask.file_constraints) or "none",
                )

                output: AgentOutput = await self._sandbox_manager.execute(
                    prompt,
                    working_dir=working_dir,
                    on_stdout_line=_on_stdout_line,
                    subtask_config=self._resolve_agent_config(subtask) or None,
                )
            # ponytail: extract stderr_tail and recent tool calls for failure context
            stderr_tail = output.stderr_tail
            if not stderr_tail and hasattr(output, "raw_stderr") and output.raw_stderr:
                stderr_tail = "\n".join(output.raw_stderr.strip().splitlines()[-10:])
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                modified_files=output.file_changes,
                summary=output.summary,
                success=output.success,
                error="" if output.success else output.summary[:2000],
                stderr_tail=stderr_tail,
                recent_tool_calls=output.tool_calls[-5:],
            )
        finally:
            # Release edit intent after execution (success or failure)
            for fp in declared_files:
                self.conflict_detector.remove_intent(fp, self.worker_id)

    async def _emit_step_event(
        self, subtask: Subtask, event_type: str, **data: Any
    ) -> None:
        """Publish a workflow step event, best-effort.

        Wraps _publish_event so a NATS/event-emitter failure never aborts
        the step chain — observability is non-fatal.
        """
        try:
            await self._publish_event(
                event_type,
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                data={"worker_id": self.worker_id, **data},
            )
        except Exception:
            logger.debug("step event publish failed", exc_info=True)

    async def _execute_steps(
        self,
        subtask: Subtask,
        working_dir: str | None,
        on_stdout_line: Any,
        context_block: str,
    ) -> AgentOutput:
        """Run a subtask's ordered multi-agent workflow steps.

        Each step runs its configured agent (claude-code / codex) with a
        prompt template. Templates support:
          {{prev_summary}} — previous step's AgentOutput.summary
          {{prev_files}}   — previous step's modified file paths (one/line)
          {{step<N>.summary}} / {{step<N>.files}} — any prior step by index

        Step 0 has no prev; {{prev_*}} resolves to empty for it. A failed
        step aborts the chain (unless abort_on_failure=False) and the
        subtask fails. The returned AgentOutput is the LAST step's output;
        file_changes are accumulated across all steps so the SubtaskResult
        reflects every file touched in the workflow.
        """
        step_outputs: list[AgentOutput] = []
        all_file_changes: list[FileChange] = []
        last_output: AgentOutput | None = None
        file_constraints_str = ", ".join(subtask.file_constraints) or "none"

        for idx, step in enumerate(subtask.steps):
            prev = step_outputs[-1] if step_outputs else None
            rendered = self._render_step_prompt(
                step.prompt,
                idx,
                step_outputs,
                prev,
                context_block,
                file_constraints_str,
            )

            # ponytail: per-step agent_config override — merge step config over
            # the subtask-level resolved config (step wins on conflict).
            base_cfg = self._resolve_agent_config(subtask) or {}
            merged_cfg = {**base_cfg, **step.agent_config} or None

            logger.info(
                "Workflow step %d/%d (subtask %s): agent=%s",
                idx + 1,
                len(subtask.steps),
                subtask.id[:8],
                step.agent,
            )
            # ponytail: emit step boundary events so TUI/dashboard can show
            # which step of the chain is running. Best-effort — a publish
            # failure must never abort the workflow.
            total = len(subtask.steps)
            percent = int(100 * idx / total) if total else 0
            await self._emit_step_event(
                subtask, "subtask_progress",
                phase=f"step {idx + 1}/{total}: {step.agent}",
                percent=percent,
                step_index=idx,
                step_total=total,
                step_agent=step.agent,
                step_status="started",
            )
            # Retry loop: attempt up to 1 + retry_count times. Only retry on
            # failure (output.success is False). Between retries, sleep
            # retry_delay_ms/1000 seconds (0 = immediate). Emit a "retrying"
            # event before each retry sleep so observers can track attempts.
            max_attempts = 1 + max(0, step.retry_count)
            output: AgentOutput | None = None
            for attempt in range(max_attempts):
                output = await self._sandbox_manager.execute(
                    rendered,
                    working_dir=working_dir,
                    on_stdout_line=on_stdout_line,
                    subtask_config=merged_cfg,
                    agent=step.agent,
                )
                if output.success or attempt == max_attempts - 1:
                    break
                # Failed and retries remain — emit retrying event + sleep.
                await self._emit_step_event(
                    subtask, "subtask_progress",
                    phase=f"step {idx + 1}/{total}: {step.agent}",
                    percent=percent,
                    step_index=idx,
                    step_total=total,
                    step_agent=step.agent,
                    step_status="retrying",
                    retry_attempt=attempt + 1,
                    step_summary=output.summary[:200],
                )
                if step.retry_delay_ms > 0:
                    await asyncio.sleep(step.retry_delay_ms / 1000)
            assert output is not None  # loop runs at least once
            step_outputs.append(output)
            # Only accumulate file changes from successful steps — a failed
            # step's partial edits are not a reliable result. ponytail: avoid
            # reporting partial work as applied when a later step succeeds.
            if output.success:
                all_file_changes.extend(output.file_changes)
            await self._emit_step_event(
                subtask, "subtask_progress",
                phase=f"step {idx + 1}/{total}: {step.agent}",
                percent=int(100 * (idx + 1) / total) if total else 0,
                step_index=idx,
                step_total=total,
                step_agent=step.agent,
                step_status="completed" if output.success else "failed",
                step_summary=output.summary[:200],
            )
            last_output = output

            if not output.success:
                if step.abort_on_failure:
                    logger.warning(
                        "Workflow step %d failed (agent=%s), aborting chain for subtask %s",
                        idx + 1,
                        step.agent,
                        subtask.id[:8],
                    )
                    # Surface the failed step's output as the subtask result.
                    return AgentOutput(
                        summary=f"[step {idx + 1} ({step.agent}) failed] {output.summary}",
                        file_changes=all_file_changes,
                        token_usage=output.token_usage,
                        success=False,
                        stderr_tail=output.stderr_tail,
                        tool_calls=output.tool_calls,
                    )
                # abort_on_failure=False: log and continue to next step.
                logger.warning(
                    "Workflow step %d failed but abort_on_failure=False; continuing chain",
                    idx + 1,
                )

        # All steps completed (or non-aborting failures). Return the last
        # step's output, with file_changes merged across the whole chain.
        if last_output is None:
            # ponytail: empty steps list shouldn't reach here (caller guards),
            # but fail safely rather than crash.
            return AgentOutput(
                summary="Workflow had no steps",
                file_changes=[],
                success=False,
            )
        return AgentOutput(
            summary=last_output.summary,
            file_changes=all_file_changes,
            token_usage=last_output.token_usage,
            success=last_output.success,
            stderr_tail=last_output.stderr_tail,
            tool_calls=last_output.tool_calls,
        )

    # Truncation limits for {{prev_outputs_json}} / {{stepN.outputs_json}}.
    # Keeps the JSON blob from blowing the agent's context window.
    _ARTIFACT_SUMMARY_MAX = 2000
    _ARTIFACT_STDERR_MAX = 1000
    _ARTIFACT_DIFF_MAX = 1000
    _ARTIFACT_TOOL_CALLS_MAX = 50

    @staticmethod
    def _output_to_json(out: AgentOutput | None) -> str:
        """Serialize an AgentOutput to a compact JSON string for prompt injection.

        Exposes summary, success, file_changes, stderr_tail, and tool_calls —
        everything a downstream agent needs to reason about the prior step's
        result. ``token_usage`` is omitted (cost/billing detail irrelevant to
        the next agent). Large fields are truncated for prompt safety.
        Returns ``"{}"`` when ``out`` is None (step 0, no predecessor).
        """
        if out is None:
            return "{}"
        fc_list = []
        for fc in out.file_changes:
            fc_list.append(
                {
                    "file_path": fc.file_path,
                    "change_type": fc.change_type.value if fc.change_type else "modified",
                    "diff": (fc.diff or "")[: Worker._ARTIFACT_DIFF_MAX],
                }
            )
        return json.dumps(
            {
                "summary": (out.summary or "")[: Worker._ARTIFACT_SUMMARY_MAX],
                "success": out.success,
                "file_changes": fc_list,
                "stderr_tail": (out.stderr_tail or "")[: Worker._ARTIFACT_STDERR_MAX],
                "tool_calls": (out.tool_calls or [])[: Worker._ARTIFACT_TOOL_CALLS_MAX],
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def _render_step_prompt(
        self,
        template: str,
        idx: int,
        step_outputs: list[AgentOutput],
        prev: AgentOutput | None,
        context_block: str,
        file_constraints_str: str,
    ) -> str:
        """Render a step prompt template, injecting prior-step outputs.

        Supported variables:
          {{prev_summary}} — previous step's summary (empty for step 0)
          {{prev_files}}   — previous step's modified file paths, one/line
          {{prev_outputs_json}} — previous step's full AgentOutput as JSON
                                   (summary, success, file_changes,
                                   stderr_tail, tool_calls; truncated).
                                   ``"{}"`` for step 0 (no predecessor).
          {{stepN.summary}} / {{stepN.files}} — step N's output (N < idx)
          {{stepN.outputs_json}} — step N's full AgentOutput as JSON (same
                                    shape as {{prev_outputs_json}}).
          {{context}}      — the subtask-level context_block
          {{file_constraints}} — comma-joined file_constraints
        """

        def _files(out: AgentOutput) -> str:
            return "\n".join(fc.file_path for fc in out.file_changes)

        prev_summary = prev.summary if prev else ""
        prev_files = _files(prev) if prev else ""
        prev_json = self._output_to_json(prev)

        rendered = template
        rendered = rendered.replace("{{prev_summary}}", prev_summary)
        rendered = rendered.replace("{{prev_files}}", prev_files)
        rendered = rendered.replace("{{prev_outputs_json}}", prev_json)
        rendered = rendered.replace("{{context}}", context_block or "")
        rendered = rendered.replace("{{file_constraints}}", file_constraints_str)

        # {{stepN.summary}} / {{stepN.files}} / {{stepN.outputs_json}} for any
        # prior step. ponytail: f-string {{ → literal {, so "{{{{...}}}}" yields
        # the double-brace token "{{stepN.summary}}" that templates use.
        for n in range(idx):
            if n < len(step_outputs):
                so = step_outputs[n]
                so_json = self._output_to_json(so)
                rendered = rendered.replace(
                    f"{{{{step{n}.summary}}}}",
                    so.summary,
                )
                rendered = rendered.replace(
                    f"{{{{step{n}.files}}}}",
                    _files(so),
                )
                rendered = rendered.replace(
                    f"{{{{step{n}.outputs_json}}}}",
                    so_json,
                )

        return rendered

    async def _broadcast_file_changes(
        self,
        subtask: Subtask,
        result: SubtaskResult,
        workspace_handle: WorkspaceHandle | None = None,
    ) -> None:
        """Broadcast file change events via NATS for distributed state sync.

        Each modified file gets a FileChangeEvent published to
        ``uc.file.changed`` so all workers and the orchestrator
        can track real-time file modifications. The event carries the
        new file content so the gateway can incrementally re-index
        without filesystem access to this worker's worktree.
        """
        if not result.modified_files or not self.nats_publisher:
            return

        # Workers use the subtask's project_id as the gateway repo_id.
        repo_id = subtask.project_id or getattr(
            getattr(self, "current_task", None), "project_id", ""
        )
        # Base path for reading the post-edit file content. Only available
        # while the workspace (worktree) is still active.
        base_path = ""
        if workspace_handle and workspace_handle.worktree_path:
            base_path = workspace_handle.worktree_path
        elif workspace_handle and workspace_handle.project_path:
            base_path = workspace_handle.project_path

        for fc in result.modified_files:
            content = ""
            if base_path and fc.change_type.value != "deleted":
                full = os.path.join(base_path, fc.file_path)
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except OSError:
                    logger.debug("Could not read post-edit content for %s", fc.file_path)
            event = FileChangeEvent(
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                file_path=fc.file_path,
                change_type=FileChangeEventType(fc.change_type.value),
                diff_summary=fc.diff[:200] if fc.diff else "",
                repo_id=repo_id,
                content=content,
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
                file_path,
                result.value,
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
            from ultimate_coders.agent.search_cache import WorkerLocalCache
            from ultimate_coders.search.query import SearchQuery

            sq = SearchQuery(subtask.description).with_modes(["hybrid"]).limit(10)
            if subtask.project_id:
                sq.in_repos([subtask.project_id])
            else:
                sq.in_all_repos(self.engine)
            d = sq.to_dict()
            cache_key = WorkerLocalCache.search_key(
                d["query"],
                d["repo_ids"],
                d["modes"],
                d["max_results"],
            )
            result = self._search_cache.get_search(cache_key)
            if result is None:
                result = self.engine.search(sq)
                if result is not None:
                    self._search_cache.put_search(cache_key, result)
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
        self,
        query: str,
        modes: list[str] | None = None,
        max_results: int = 20,
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
        self,
        key: str,
        project_id: str = "",
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
            key_scope=scope,
            key=key,
            project_id=pid or None,
        )

    def write_shared_memory(
        self,
        key: str,
        content: str,
        project_id: str = "",
        content_type: str = "text",
        importance: float = 0.7,
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
            MemoryEntry or None — None if the engine is unavailable or the
            underlying write raised (mirrors delete_shared_memory: a failure is
            non-fatal and skips the NATS broadcast since nothing was written).
        """
        if self.engine is None:
            return None
        pid = project_id or getattr(self.current_task, "project_id", "")
        scope = "project" if pid else "global"
        try:
            result = self.engine.write_memory(
                key_scope=scope,
                key=key,
                content=content,
                content_type=content_type,
                source_agent=f"worker:{self.worker_id}",
                importance=importance,
                tags=tags,
                project_id=pid or None,
            )
        except Exception:
            logger.warning("write_shared_memory failed for key=%s", key, exc_info=True)
            return None  # ponytail: nothing written — skip broadcast, non-fatal
        # Broadcast memory change via NATS for cross-Worker cache invalidation.
        # write_shared_memory is sync, so fire-and-forget onto the running loop
        # when one exists; if called outside a loop the broadcast is skipped
        # (the next cache miss / TTL expiry still converges).
        if result is not None:
            self._broadcast_memory_changed(pid, key, "write")
        return result

    def delete_shared_memory(self, key: str, project_id: str = "") -> bool:
        """Delete project-scoped memory (shared across Workers via Gateway).

        Mirrors ``write_shared_memory``: routes to ``engine.delete_memory`` and
        broadcasts ``uc.memory.changed`` (action='delete') so other Workers
        invalidate stale search-cache entries.

        Returns:
            True if the delete succeeded (no exception), False if the engine is
            unavailable or the underlying call raised.
        """
        if self.engine is None:
            return False
        pid = project_id or getattr(self.current_task, "project_id", "")
        scope = "project" if pid else "global"
        try:
            self.engine.delete_memory(
                key_scope=scope,
                key=key,
                project_id=pid or None,
            )
        except Exception:
            logger.warning("delete_shared_memory failed for key=%s", key, exc_info=True)
            return False  # ponytail: nothing deleted — skip broadcast, no convergence needed
        # Broadcast the delete so other Workers drop stale cache entries.
        self._broadcast_memory_changed(pid, key, "delete")
        return True

    def _broadcast_memory_changed(
        self,
        project_id: str,
        key: str,
        action: str,
    ) -> None:
        """Fire-and-forget a uc.memory.changed broadcast onto the running loop.

        Best-effort: skipped (no error) when called outside a running loop —
        the next cache miss / TTL expiry still converges. The scheduled task is
        kept in ``self._bg_tasks`` so asyncio's weak ref doesn't GC it before it
        completes (CPython documented trap for unreferenced create_task()).
        """
        if self.nats_publisher is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # ponytail: no running loop — skip, TTL converges
        task = loop.create_task(
            self.nats_publisher.publish_memory_changed(
                project_id=project_id,
                key=key,
                action=action,
                source_worker=self.worker_id,
            ),
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
