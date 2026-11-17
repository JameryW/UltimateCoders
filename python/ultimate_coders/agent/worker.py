"""Worker — executes subtasks via sandbox coding agents.

Previously supported an LLM tool-calling mode — removed in favor of
sandbox-only execution. Coding agents (Grok Build, Claude Code, Codex) have
their own tool chains and don't need a Python-side tool-calling loop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictResult,
    EditIntent,
    EditType,
    LineRange,
)
from ultimate_coders.agent.context_compiler import (
    compile_context,
    from_gateway_context_block,
    from_hy_memory_block,
    from_search_block,
)
from ultimate_coders.agent.contract import contract_digest
from ultimate_coders.agent.hy_memory import build_hy_memory_context_block
from ultimate_coders.agent.llm import (
    LLMRetryExhaustedError,
    _classify_llm_error,
)
from ultimate_coders.agent.progress import AttemptEvidence, ProgressTracker
from ultimate_coders.agent.push_status import (
    TRANSIENT_PUSH_STATUSES,
    UNCONFIRMED_PUSH_STATUSES,
)
from ultimate_coders.agent.review import REVIEW_INSTRUCTIONS, parse_review
from ultimate_coders.agent.routing import ModelRouter, classify_failure
from ultimate_coders.agent.sandbox import (
    AgentOutput,
    SandboxConfig,
    SandboxManager,
    subtask_usage_from_token_usage,
)
from ultimate_coders.agent.stall_redispatch import build_stall_signal
from ultimate_coders.agent.state_sync import (
    ContextInjector,
    FileChangeEvent,
    FileChangeEventType,
)
from ultimate_coders.agent.step_condition import ConditionError, evaluate
from ultimate_coders.agent.types import (
    FileChange,
    StepUsage,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    WorkerInfo,
    WorkflowStep,
)
from ultimate_coders.agent.workspace import WorkspaceHandle, WorkspaceManager
from ultimate_coders.architecture.mandate import (
    Mandate,
    MandateError,
    mandate_envelope_enabled,
)
from ultimate_coders.knowledge import rollout
from ultimate_coders.knowledge.attempt_chain import (
    AttemptChainLedger,
    AttemptRound,
    locate_symbols,
)
from ultimate_coders.knowledge.claims import (
    ACCEPTED_MERGE_STATUSES,
    attempt_scope,
    record_delivery_claim,
    record_failure_claim,
    validate_and_promote_delivery,
)
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.model import AttemptRef, EvidenceKind, KnowledgeScope, safe_text
from ultimate_coders.knowledge.recovery import (
    RecoveryView,
    build_recovery_view,
    recovery_context_items,
)
from ultimate_coders.knowledge.retrieval import knowledge_context_items
from ultimate_coders.knowledge.signatures import normalize_failure_signature
from ultimate_coders.knowledge.verdicts import VerificationReport, extract_verification

#: Worktree ``git status`` change types mapped onto the patch-ledger vocabulary
#: (``FilePatch.change_type``): the workspace capture says what happened to the
#: file, the ledger says how the diff represents it.
_DIFF_CHANGE_TYPES = {
    "created": "added",
    "added": "added",
    "deleted": "deleted",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "copied",
}


logger = logging.getLogger(__name__)


def _is_test_file(path: Path) -> bool:
    """Whether a changed path is a Python test file the process review covers."""

    name = path.name
    return name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py")

#: Release verdicts that mean the accepted work did not reach the remote.
_DELIVERY_TERMINAL_FAILURES = ("conflict", "commit_failed")


def _delivery_verdict(merge_result: dict[str, Any]) -> tuple[bool, bool]:
    """Return ``(failed, retryable)`` for a workspace release result.

    ``push_status`` "unknown" (a publish awaiting reconciliation) and "busy" (the
    effect claim is held elsewhere) mean the branch may not be on the remote at
    all. They are transient, so the subtask must be retried -- the retry is what
    runs reconciliation and settles the publish -- while a conflict, a failed
    commit or a definite push failure is not retryable.

    The verdict set comes from ``agent.push_status`` so that a status added by a
    producer is classified here rather than silently treated as success (T68).
    """
    if merge_result.get("status") in _DELIVERY_TERMINAL_FAILURES:
        return True, False
    push_status = merge_result.get("push_status")
    if push_status in UNCONFIRMED_PUSH_STATUSES:
        return True, push_status in TRANSIENT_PUSH_STATUSES
    return False, False


DEFAULT_SUBTASK_TIMEOUT_SECONDS = 600
SUBTASK_TIMEOUT_ENV_VAR = "UC_SUBTASK_TIMEOUT_SECONDS"
DESCRIPTION_PROFILES_ENV_VAR = "UC_DISABLE_DESCRIPTION_PROFILES"


def _default_subtask_timeout_seconds() -> int:
    """Return the default per-subtask timeout, overridable by env.

    Long content phases routinely exceed ten minutes; deployments can raise
    the default without changing every plan.
    """
    raw = os.environ.get(SUBTASK_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_SUBTASK_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r; using %d",
            SUBTASK_TIMEOUT_ENV_VAR,
            raw,
            DEFAULT_SUBTASK_TIMEOUT_SECONDS,
        )
        return DEFAULT_SUBTASK_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(
            "Ignoring non-positive %s=%r; using %d",
            SUBTASK_TIMEOUT_ENV_VAR,
            raw,
            DEFAULT_SUBTASK_TIMEOUT_SECONDS,
        )
        return DEFAULT_SUBTASK_TIMEOUT_SECONDS
    return value


def _description_profiles_enabled() -> bool:
    """Whether subtask-description keyword profiles are enabled."""
    raw = os.environ.get(DESCRIPTION_PROFILES_ENV_VAR, "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


async def _engine_call(
    engine: Any,
    sync_name: str,
    async_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call an Engine method without blocking the event loop.

    ponytail: F59 — in gRPC mode the sync engine API is a blocking RPC
    (up to grpc_timeout_seconds=30), which froze the worker's whole loop —
    stalling the 30s NATS heartbeat toward the gateway's 90s stale-worker
    eviction. Prefer the engine's ``*_async`` variant when present; fall
    back to the sync call for engines without one (in-memory engine /
    legacy test mocks — fast and non-blocking anyway).
    """
    # ponytail: iscoroutinefunction guard — plain MagicMocks auto-create the
    # *_async attribute as a non-coroutine; treat those as sync engines.
    async_fn = getattr(engine, async_name, None)
    if async_fn is not None and inspect.iscoroutinefunction(async_fn):
        return await async_fn(*args, **kwargs)
    return getattr(engine, sync_name)(*args, **kwargs)


# ponytail: map mcp:<server> tags to semantic capability aliases.
# Add rows as more in-process MCP servers land.
_MCP_CAP_ALIASES: dict[str, str] = {
    "mcp:uc-fs": "file-edit",
    "mcp:uc-engine": "search",  # uc-engine already implies search; no-op if present
    "mcp:uc-lsp": "lsp",  # uc-lsp provides real-time LSP symbol tools
}

_SUBTASK_USER_TEMPLATE = """\
Original user request and constraints: {user_request}

Current subtask: {description}
Complete only the current subtask while honoring the original request.

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
            if obj.get("event") == "inference_phase":
                return "subtask_progress", {
                    "phase": obj.get("phase"),
                    "experiment_id": obj.get("experiment_id"),
                    "cleanup_pending": obj.get("cleanup_pending", False),
                }
            if obj.get("event") == "drift_advisor":
                return "subtask_progress", {
                    "phase": "drift advisor",
                    "percent": 0,
                    "drift_signals": obj.get("signals", []),
                    "drift_prompt": str(obj.get("prompt") or "")[:500],
                }
            if obj.get("event") == "radio_post":
                message = obj.get("message") or {}
                return "subtask_progress", {
                    "phase": "agent radio",
                    "percent": 0,
                    "radio_message_id": str(message.get("message_id") or "")[:64],
                    "radio_kind": str(message.get("kind") or ""),
                    "radio_node": str(message.get("node_id") or "")[:64],
                    "radio_text": str(message.get("text") or "")[:300],
                }
            if obj.get("event") == "teammate_radio_update":
                return "subtask_progress", {
                    "phase": "teammate radio update",
                    "percent": 0,
                    "radio_update": str(obj.get("text") or "")[:500],
                }
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


def _step_usage(
    step_index: int,
    parallel_group: str,
    agent: str,
    output: AgentOutput,
) -> StepUsage:
    """Build one step's usage record from its identity and its output (T18 #668).

    The single construction point for ``payload.steps[]`` entries, so the three
    invariants that make that array trustworthy live in one place:

    * ``step_index`` is the **caller's** — the position in the subtask's
      execution sequence, never a re-count of the collected list. Re-counting
      would erase the gap that is how a skipped step (condition false) shows
      up; a skipped step produces no entry at all.
    * ``usage`` is ``None`` when the adapter reported nothing. It is never a
      zeroed block: ``subtask_usage_from_token_usage`` already returns ``None``
      for an absent block, and synthesising one here would put a real
      measurement where the adapter said nothing (D13 #657).
    * ``source`` prefers the adapter's own stamp (``usage.source``, set at its
      parse site) and falls back to the step's declared ``agent`` — so a step
      that reported no numbers still names who ran it, which is the only thing
      left to name it by once ``usage`` is ``None``.
    """
    usage = subtask_usage_from_token_usage(output.token_usage)
    source: str | None = None
    if usage is not None and usage.source:
        source = usage.source
    elif agent:
        source = agent
    return StepUsage(
        step_index=step_index,
        parallel_group=parallel_group,
        usage=usage,
        source=source,
    )


class Worker:
    """Executes subtasks via sandbox agent.

    Usage:
        worker = Worker(engine=engine, sandbox_config=config)
        result = await worker.execute_subtask(subtask)
    """

    # T12 #654 / D12 #649 — bound on the affinity recent-files summary this
    # worker advertises on every heartbeat. Twin of the gateway's
    # ``uc_grpc::placement::MAX_RECENT_FILES``; both sides bound defensively
    # because the wire is untrusted in each direction.
    MAX_RECENT_FILES: int = 64

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
        model_router: Any = None,
    ):
        self.worker_id = worker_id or os.environ.get("UC_WORKER_ID") or str(uuid.uuid4())
        self.engine = engine
        if sandbox_config is None:
            from ultimate_coders.agent.network_mode import resolve_network_default

            # T16 — opt-in default Restricted; explicit SandboxConfig wins.
            self._sandbox_config = SandboxConfig(network=resolve_network_default())
        else:
            self._sandbox_config = sandbox_config
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
        # Compact description (counts, drops, cost; never content) of the
        # last compiled context manifest, surfaced on subtask_started.
        self._last_context_manifest: dict[str, Any] | None = None
        # P5 read face: counts-only summary of graph knowledge injected into
        # this attempt's context (empty unless stage >= graph).
        self._last_knowledge_summary: dict[str, Any] = {}

        # Model routing (agent-os-architecture.md §5). The router answers
        # two questions per attempt: which model/budget this (stage, risk)
        # should use, and whether a failure is worth another LLM call. The
        # decision is recorded on subtask_started / retry events so the
        # routing choice is auditable even where the sandboxed agent owns
        # the actual call; with no UC_ROUTE_TABLE configured every decision
        # is the identity and the event fields are omitted entirely.
        # ``model_router`` is injectable so tests describe a table instead
        # of the process environment.
        self._model_router: Any = model_router if model_router is not None else ModelRouter()
        self._last_route: Any = None

        # Self-heartbeat monitoring — track last heartbeat timestamp
        # so stale_worker_cleanup can detect local worker stalls
        self._last_heartbeat_at: datetime = datetime.now(timezone.utc)

        # T12 #654 / D12 #649 — bounded, newest-first summary of the files
        # this worker has recently touched. Advertised on every heartbeat as
        # the affinity-placement input: the gateway scores a node's declared
        # file_constraints against it to pick WHERE a node goes first. Pure
        # advisory state — never used to gate execution.
        self._recent_files: list[str] = []

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
        # T16 (#661): "review" is deliberately NOT a default capability.
        #
        # D14 requires that the worker which produced a result must not also
        # review it. T19 (#670) has since added the dispatch-side exclusion
        # primitive, so independence is now enforced in two places; this one
        # stays because it is the cheaper of the two — a review node requires
        # the "review" capability, so a worker that never opted in cannot even
        # become a candidate, and the exclusion set only ever has to reason
        # about workers that did opt in.
        # Advertising it by default would let every producing worker claim its
        # own review, i.e. turn review into self-assessment with no code change
        # anywhere else.
        # Opt in with UC_CAP_REVIEW (same pattern as UC_CAP_BROWSER/DEBUG).
        caps = ["code", "search", "memory", "test", "decompose"]
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
        for flag, cap in (
            ("UC_CAP_BROWSER", "browser"),
            ("UC_CAP_DEBUG", "debug"),
            # T16 (#661): reviewer designation is opt-in — see the note on the
            # default capability list above.
            ("UC_CAP_REVIEW", "review"),
        ):
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
        # ponytail: probe agent availability via the plugin registry — CLI
        # agents advertise only when their binary is on PATH, API-backed
        # harnesses (e.g. deepseek-harness) always advertise. New agents
        # registered as plugins show up here without touching this method.
        from ultimate_coders.agent import registry as agent_registry
        from ultimate_coders.agent.registry import ensure_builtin_plugins

        ensure_builtin_plugins()
        caps.extend(agent_registry.registry.capability_names(shutil.which))

        caps.append("inference_benchmark")
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for c in caps:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique

    async def refresh_inference_capabilities(self) -> bool:
        from ultimate_coders.inference.agent import InferenceInfraAgent

        capabilities = await InferenceInfraAgent.probe()
        remote = set(InferenceInfraAgent.capabilities) | {"inference_benchmark"}
        updated = [cap for cap in self.capabilities if cap not in remote] + capabilities
        changed = updated != self.capabilities
        self.capabilities = updated
        return changed

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

    def _adapter_candidates(self) -> list[str]:
        """Discoverable adapter names for complementary retry routing."""

        from ultimate_coders.agent.registry import registry

        names: set[str] = set()
        for name in registry.available():
            spec = registry.get_spec(name)
            if spec is not None and spec.discoverable:
                names.add(spec.name)
        return sorted(names)

    def _route_retry_adapter(
        self,
        subtask: Subtask,
        *,
        last_failure: str,
        failure_kind: str,
    ) -> Any | None:
        """Select a complementary adapter for the next attempt (opt-in).

        The candidate list comes from the plugin registry and the behavior
        profiles come from ``UC_ADAPTER_PROFILES_JSON`` (the durable table is
        the PostgreSQL mirror documented by behavior_router).  Nothing is
        changed when the flag is off, when there is only one adapter, or when
        the router cannot find an alternative.
        """

        from ultimate_coders.architecture.behavior_router import (
            BehaviorProfileStore,
            ComplementaryAdapterRouter,
            behavior_routing_enabled,
        )

        if not behavior_routing_enabled():
            return None
        candidates = self._adapter_candidates()
        if len(candidates) < 2:
            return None
        store = BehaviorProfileStore()
        raw_profiles = os.environ.get("UC_ADAPTER_PROFILES_JSON", "").strip()
        if raw_profiles:
            try:
                rows = json.loads(raw_profiles)
            except ValueError:
                rows = []
            if isinstance(rows, list):
                store.load_rows(row for row in rows if isinstance(row, dict))
            elif isinstance(rows, dict):
                store.load_rows((rows,))
        failed = str(
            getattr(subtask, "_last_agent", "")
            or (subtask.agent_config or {}).get("agent", "")
            or ""
        )
        choice = ComplementaryAdapterRouter(store).choose(
            subtask.description,
            failed_adapter=failed,
            candidates=candidates,
            failure_kind=failure_kind,
            failure_text=last_failure,
        )
        if choice is None:
            return None
        updated = dict(subtask.agent_config or {})
        updated["agent"] = choice.adapter
        subtask.agent_config = updated
        logger.info(
            "Subtask %s retry adapter %s -> %s (score %.3f: %s)",
            subtask.id[:8],
            failed or "?",
            choice.adapter,
            choice.score,
            choice.reason,
        )
        return choice

    def _drift_hints(self, subtask: Subtask) -> dict[str, str]:
        """Task-scoped boundary/coverage hints for the local harness monitor.

        ``file_constraints`` is the worker's declared edit scope; the harness
        normally has no planner contract, so these env hints make the drift
        monitor observe the same boundary instead of treating every path as
        in-scope.  They matter only for ``local-harness``/``local-llm``.
        """

        files = [str(path) for path in (subtask.file_constraints or []) if str(path).strip()]
        if not files:
            return {}
        joined = ",".join(files)
        return {
            "_uc_drift_boundary": joined,
            "_uc_drift_required_files": joined,
        }

    def _radio_hints(self, subtask: Subtask) -> dict[str, str]:
        """Graph/node hints for the opt-in local AgentRadio tools."""

        from ultimate_coders.agent.radio import radio_enabled

        if not radio_enabled():
            return {}
        graph_id = str(getattr(subtask, "parent_id", "") or subtask.id or "").strip()
        node_id = str(subtask.id or "").strip()
        if not graph_id or not node_id:
            return {}
        return {
            "_uc_radio_graph_id": graph_id,
            "_uc_radio_node_id": node_id,
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
        Deployments can disable the heuristic entirely with
        ``UC_DISABLE_DESCRIPTION_PROFILES=1``; this matters for multi-step
        plans where a keyword in the subtask description would otherwise
        force the read-only review profile onto write-capable steps.
        """
        if not _description_profiles_enabled():
            return None
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

    def record_recent_files(self, paths: Any) -> None:
        """Record the files a finished subtask touched (T12 #654).

        Newest-first, de-duplicated, capped at :attr:`MAX_RECENT_FILES`.
        Re-touching an older path moves it back to the front, so the list is
        a recency ordering rather than an insertion log — exactly what the
        affinity score wants. Within a single call the first entry is treated
        as the most recent, which is why callers pass their primary signal
        first (a node's declared ``file_constraints`` ahead of its diff).

        Blank/whitespace-only entries are dropped. Never raises: this is
        advisory metadata, and a malformed path must not fail an execution
        that otherwise succeeded.
        """
        try:
            incoming = [str(p).strip() for p in (paths or []) if p is not None and str(p).strip()]
        except Exception:
            logger.debug("recent-files recording skipped", exc_info=True)
            return
        if not incoming:
            return
        # Move-to-front de-duplication, preserving first occurrence order.
        fresh: list[str] = []
        seen: set[str] = set()
        for path in incoming:
            if path in seen:
                continue
            seen.add(path)
            fresh.append(path)
        kept = [p for p in self._recent_files if p not in seen]
        self._recent_files = (fresh + kept)[: self.MAX_RECENT_FILES]

    def recent_files(self) -> list[str]:
        """Snapshot of the recent-files summary (newest first, bounded)."""
        return list(self._recent_files)

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

    def kill_node(self, task_id: str, subtask_id: str) -> bool:
        """T7 #643 cooperative cancel: kill the agent process group for one
        node (the sandbox registers spawned subprocesses under
        (task_id, node_id) via the execute cancel_key).

        Returns True when a live process was found and killed. Any late
        result from the killed execution stays fenced on the graph plane.
        """
        kill = getattr(self._sandbox_manager, "kill_group", None)
        if kill is None:
            return False
        try:
            return kill((task_id, subtask_id))
        except Exception:
            logger.warning(
                "kill_node failed for task=%s node=%s",
                task_id[:8],
                subtask_id[:8],
                exc_info=True,
            )
            return False

    async def execute_subtask(
        self,
        subtask: Subtask,
        gateway_context_block: dict | None = None,
    ) -> SubtaskResult:
        """Execute a subtask via sandbox agent.

        Includes workspace isolation, context injection, checkpoint support,
        and automatic retry with backoff on failure:
        - Acquires a workspace (git worktree) for file-modifying subtasks
        - Injects completed dependency context into the subtask prompt
        - Saves intermediate results as checkpoints for resume
        - Broadcasts file change events for distributed state sync
        - Retries up to MAX_RETRIES times with backoff on failure

        T10 #652 / D10 #647: ``gateway_context_block`` is the gateway-composed
        dependency context from the dispatch envelope (dict with ``entries`` +
        ``truncated``). When present it is preferred over the local
        ``_context_injector`` — the graph plane's committed outputs are the
        single source of truth, and worker-only mode (no Orchestrator state)
        gains dependency context. When absent (in-flight envelopes, legacy
        gateway) the local injector fallback keeps behavior identical to
        before.
        """
        self.current_task = subtask
        self._active_count += 1
        subtask.status = SubtaskStatus.IN_PROGRESS
        workspace_handle = None
        needs_worktree = False

        try:
            # Check for an existing result for THIS ATTEMPT — skip if already
            # completed. T4 #640: the key is attempt-scoped; a subtask-scoped
            # key was a real bug, because T3's fence -> READY -> re-dispatch
            # mints a new attempt whose execution would hit the previous
            # attempt's successful checkpoint and return a stale result
            # without running anything at all.
            checkpoint = await self._load_checkpoint(subtask)
            checkpoint_review = (
                parse_review(json.dumps(checkpoint.get("review"))) if checkpoint else None
            )
            if (
                checkpoint
                and "review" in subtask.required_capabilities
                and (
                    checkpoint_review is None
                    or not checkpoint_review.approved
                    or checkpoint.get("modified_files")
                )
            ):
                # Old checkpoints without a verdict are not evidence of an
                # approved review. Re-execute instead of replaying false success.
                checkpoint = None
            if (
                checkpoint is not None
                and checkpoint.get("delivery") != "pending"
                and (checkpoint.get("success") or checkpoint.get("retryable") is False)
            ):
                logger.info(
                    "Subtask %s attempt %s already has a result, replaying it "
                    "instead of re-executing (duplicate delivery)",
                    subtask.id[:8],
                    subtask.dispatch_retry_count,
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
                    success=checkpoint.get("success", True),
                    retryable=checkpoint.get("retryable", True),
                    cleanup_pending=checkpoint.get("cleanup_pending", False),
                    domain_result=checkpoint.get("domain_result"),
                    modified_files=mod_files,
                    recent_tool_calls=checkpoint.get("tool_calls", []),
                    stderr_tail=checkpoint.get("stderr_tail", ""),
                    review=checkpoint_review,
                )

            # Inject context from completed dependencies. The gateway-composed
            # block (committed graph outputs) is authoritative when the
            # dispatch carried one; otherwise the local injector supplies the
            # dependency results (in-flight envelopes / legacy gateway).
            # Route the first attempt before compiling context: the routing
            # decision carries the risk level that sizes the context budget
            # allocation (high risk reserves a share for verified evidence).
            # Retries re-route inside the loop (recovery stage).
            self._last_route = self._model_router.route_subtask(
                subtask.description,
                attempt=0,
                file_constraints=subtask.file_constraints,
                capabilities=subtask.required_capabilities,
            )
            self._last_context_manifest = None
            search_block = await self._build_search_context(subtask)
            # Opt-in Hy-Memory auto-context (T12 / UC_HY_MEMORY_AUTO_CONTEXT).
            # Soft-skip when flag off, package missing, or recall empty.
            hy_block = build_hy_memory_context_block(subtask.description or "")
            if gateway_context_block is not None:
                source_items = from_gateway_context_block(gateway_context_block)
                legacy_style = "gateway"
                gateway_truncated = bool(
                    gateway_context_block.get("truncated")
                    if isinstance(gateway_context_block, dict)
                    else False
                )
            else:
                source_items = self._context_injector.dependency_items(subtask.depends_on)
                legacy_style = "worker"
                gateway_truncated = False
            # One budget across every source: dependency/gateway results (P3),
            # search hits (P2), and optional Hy-Memory (P4) compete for the
            # same tokens instead of truncating independently. The rendered
            # text preserves each path's legacy wording; the routed risk
            # decides how the budget is allocated between tiers.
            #
            # Kept as the attempt's *base* items: a retry appends recovery
            # evidence to this list and recompiles, so recovery facts obey the
            # same budget and dedupe instead of being pasted onto a finished
            # prompt.
            context_source_items = (
                list(source_items)
                + from_search_block(search_block)
                + from_hy_memory_block(hy_block)
            )
            # P5 read face: gate-approved claims shared inside this task graph.
            # Empty unless stage >= graph; best-effort, counts-only summary.
            knowledge_items, knowledge_summary = await self._knowledge_context_items(subtask)
            if knowledge_items:
                context_source_items = list(context_source_items) + knowledge_items
            self._last_knowledge_summary = knowledge_summary
            manifest = compile_context(
                context_source_items,
                budget_tokens=self._context_budget_tokens(),
                risk=self._last_route.risk,
            )
            context_block = manifest.render_legacy(
                legacy_style=legacy_style, gateway_truncated=gateway_truncated
            )
            self._last_context_manifest = manifest.summary()
            assembled_context = self._assembled_context_block(manifest)
            if assembled_context is not None:
                context_block, assembly_summary = assembled_context
                self._last_context_manifest["assembly"] = assembly_summary
            # Audit trail: the same summary that rides the started event also
            # lands in the runtime state, so the "why was the prompt smaller"
            # question outlives the event stream. The gateway path records one
            # now too -- a compiled prompt with no manifest is an audit hole.
            await self._record_context_manifest(subtask, self._last_context_manifest)

            # Acquire workspace if this subtask modifies files
            from ultimate_coders.inference.agent import InferenceInfraAgent

            routed = InferenceInfraAgent.route(
                subtask.description, self._resolve_agent_config(subtask)
            )
            if routed:
                subtask.agent_config = routed
            needs_worktree = InferenceInfraAgent.requires_workspace(
                subtask.agent_config,
                subtask.steps,
            )
            if needs_worktree and self._workspace_manager is None:
                from ultimate_coders.agent.workspace import WorkspaceManager

                self._workspace_manager = WorkspaceManager(self._sandbox_config.project_path)
            # Decision Record start after route + context (+ any routed
            # agent_config governance), before workspace acquire: an
            # allocation failure still finishes the attempt and must leave
            # an attempt-scoped audit join (D5 / spec-p0).
            await self._record_decision_start(subtask)
            workspace_handle = None
            if self._workspace_manager and (subtask.file_constraints or needs_worktree):
                if needs_worktree:
                    self._workspace_manager.bind_runtime_state(await self._domain_state())
                workspace_handle = await self._workspace_manager.acquire(
                    subtask.id,
                    **({"require_worktree": True} if needs_worktree else {}),
                )
                if needs_worktree and workspace_handle is None:
                    failed = SubtaskResult(
                        subtask_id=subtask.id,
                        worker_id=self.worker_id,
                        success=False,
                        retryable=False,
                        summary="Exclusive inference worktree allocation failed",
                        error="Exclusive inference worktree allocation failed",
                    )
                    await self._record_decision_terminal(subtask, failed)
                    return failed
                if workspace_handle:
                    logger.info(
                        "Subtask %s allocated workspace %s",
                        subtask.id[:8],
                        workspace_handle.workspace_id,
                    )

            # The first attempt's route was computed before context
            # compilation (its risk sizes the budget allocation); the
            # started event below carries it.
            started_data: dict[str, Any] = {
                "description": subtask.description,
                "worker_id": self.worker_id,
            }
            if self._last_context_manifest is not None:
                # Provenance evidence: budget, per-tier counts and drop
                # reasons for the prompt this attempt will run with. No
                # content — the manifest summary is log-safe.
                started_data["context"] = self._last_context_manifest
            if self._last_route is not None and not self._last_route.is_identity:
                # Which (stage, risk) this attempt was routed at, with the
                # model and budget the router chose. Omitted entirely when
                # no table is configured, so the default event is unchanged.
                started_data["route"] = self._last_route.to_dict()
            if self._last_knowledge_summary:
                # Counts-only: how much graph knowledge the read face injected
                # (and whether the retrieval budget truncated it).
                started_data["knowledge"] = self._last_knowledge_summary
            await self._publish_event(
                "subtask_started",
                task_id=subtask.parent_id,
                subtask_id=subtask.id,
                data=started_data,
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
            #: The previous attempt's error text, consumed by the router:
            #: a schema failure re-routes at the recovery stage with a
            #: reduced budget instead of repeating the failed request.
            last_failure = ""
            #: Log-safe summary of the recovery view compiled into the current
            #: attempt ({} when the rollout stage is disabled or no prior round
            #: could be read). Rides the retry event, never the prompt.
            recovery_summary: dict[str, Any] = {}
            progress_tracker = ProgressTracker(stall_limit=self._progress_stall_limit())
            for attempt in range(self.MAX_RETRIES):
                if attempt > 0:
                    # Re-route: every attempt after the first is recovery,
                    # and a schema failure shrinks the budget for the
                    # re-issue (lightweight repair, not an identical retry).
                    self._last_route = self._model_router.route_subtask(
                        subtask.description,
                        attempt=attempt,
                        last_failure=last_failure,
                        file_constraints=subtask.file_constraints,
                        capabilities=subtask.required_capabilities,
                    )
                    # Recovery view: a retry is not a blind replay of the first
                    # attempt. When the rollout stage allows it, compile the
                    # committed patch/failure facts of the prior attempt(s) into
                    # this attempt's context (same budget, same manifest), so
                    # the next agent sees what was already tried and why it
                    # failed instead of only a free-text error line.
                    context_block, recovery_summary = await self._recovery_context_block(
                        subtask,
                        attempt,
                        context_source_items,
                        context_block,
                        legacy_style=legacy_style,
                        gateway_truncated=gateway_truncated,
                    )
                # Re-acquire workspace each attempt: prior attempt released it
                # (or never had one). Without this, retry runs against a freed
                # worktree and double-releases. ponytail: per-attempt acquire
                if (
                    self._workspace_manager
                    and (subtask.file_constraints or needs_worktree)
                    and workspace_handle is None
                ):
                    workspace_handle = await self._workspace_manager.acquire(
                        subtask.id,
                        **({"require_worktree": True} if needs_worktree else {}),
                    )
                    if needs_worktree and workspace_handle is None:
                        failed = SubtaskResult(
                            subtask_id=subtask.id,
                            worker_id=self.worker_id,
                            success=False,
                            retryable=False,
                            summary="Exclusive inference worktree allocation failed",
                            error="Exclusive inference worktree allocation failed",
                        )
                        await self._record_decision_terminal(subtask, failed)
                        return failed
                    if workspace_handle:
                        logger.info(
                            "Subtask %s re-acquired workspace %s (attempt %d)",
                            subtask.id[:8],
                            workspace_handle.workspace_id,
                            attempt + 1,
                        )
                try:
                    timeout_secs = subtask.timeout_seconds or _default_subtask_timeout_seconds()
                    try:
                        await _progress("executing", 50)
                        if checkpoint and checkpoint.get("delivery") == "pending":
                            from ultimate_coders.agent.types import ChangeType, FileChange

                            result = SubtaskResult(
                                subtask_id=subtask.id,
                                success=True,
                                summary=checkpoint["summary"],
                                domain_result=checkpoint.get("domain_result"),
                                modified_files=[
                                    FileChange(
                                        file_path=item["file_path"],
                                        change_type=ChangeType(item["change_type"]),
                                    )
                                    for item in checkpoint["modified_files"]
                                ],
                            )
                        else:
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
                            retryable=not needs_worktree,
                            cleanup_pending=needs_worktree,
                            error=f"Subtask timed out after {timeout_secs}s",
                        )

                    # Attempt diff-chain: snapshot what this attempt actually
                    # changed while the worktree still holds it. Captured
                    # before release/merge -- once the workspace is merged the
                    # candidate diff is no longer inspectable, and a failed
                    # attempt's patch is exactly what the next retry needs.
                    await self._record_attempt_round(
                        subtask, attempt, result, workspace_handle
                    )
                    # Conclusions ride the evidence ledger, not the patch round:
                    # the round never claims a verdict the sandbox did not prove.
                    await self._record_attempt_evidence(subtask, attempt, result)

                    if needs_worktree and result.success:
                        await self._save_checkpoint(subtask, result, delivery="pending")

                    # Release workspace (merge if successful)
                    if result.success and result.modified_files:
                        await self._broadcast_file_changes(subtask, result, workspace_handle)
                    if workspace_handle and result.cleanup_pending:
                        await self._workspace_manager.quarantine(workspace_handle)
                    if workspace_handle and not result.cleanup_pending:
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
                        delivery_failed, delivery_retryable = _delivery_verdict(merge_result)
                        if result.success and delivery_failed:
                            result.success, result.retryable = False, delivery_retryable
                            result.summary = (
                                "Code delivery unconfirmed; workspace preserved"
                                if delivery_retryable
                                else "Accepted code delivery failed; workspace preserved"
                            )
                            result.error = result.summary
                        if result.domain_result:
                            result.domain_result["delivery"] = merge_result
                            await self._record_inference_delivery(
                                result.domain_result, merge_result
                            )
                        # Integration receipt: what the target accepted and what
                        # the remote said about the branch push. Promotion later
                        # requires this evidence; the worker's success flag is
                        # not it (principle B).
                        delivery_evidence = await self._record_delivery_evidence(
                            subtask, attempt, merge_result
                        )
                        await self._record_delivery_claim(
                            subtask, attempt, result, merge_result, delivery_evidence
                        )
                        workspace_handle = None  # ponytail: freed; re-acquire next attempt

                    # Success is replayable only after code delivery completed.
                    await self._save_checkpoint(subtask, result)
                    self._context_injector.add_result(
                        subtask_id=subtask.id,
                        summary=result.summary,
                        modified_files=[f.file_path for f in result.modified_files],
                        success=result.success,
                    )
                    if result.success and result.domain_result:
                        await self.write_shared_memory(
                            key=f"inference:{subtask.parent_id}:{subtask.id}",
                            content=json.dumps(result.domain_result, ensure_ascii=False),
                            project_id=subtask.project_id,
                            content_type="structured",
                            tags=["inference_infra", "benchmark", "adaptation_graph"],
                        )

                    if result.success:
                        await _progress("finalizing", 95)
                        await self._record_decision_terminal(subtask, result)
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

                        # Retry transient/unknown failures, but surface
                        # permanent auth/configuration failures immediately.
                        # A missing CLI login is deterministic and retrying it
                        # three times only burns time before showing the same
                        # actionable error in the dashboard. A schema failure
                        # is worth ONE reduced retry (routing.py classifies
                        # it as a lightweight repair) -- the request was
                        # malformed, not the goal.
                        failure_action = classify_failure(result.error or result.summary)
                        failure_data["failure_action"] = failure_action.action
                        # A retry that repeats the same failure verbatim is
                        # budget burn, not persistence: gate it on evidence
                        # movement, not on the attempt counter alone.
                        verdict = progress_tracker.observe(
                            AttemptEvidence.from_result(result, attempt=attempt)
                        )
                        failure_data["progress"] = {
                            "action": verdict.action,
                            "stalled_streak": verdict.stalled_streak,
                            "reason": verdict.reason,
                        }
                        if verdict.action == "stop":
                            # Keep loop_detected for compat; attach structured
                            # stall_signal so consumers need not scrape logs.
                            # Agent swap is owned by the orchestrator (T14 /
                            # Approach A) behind UC_STALL_REDISPATCH_ENFORCE.
                            failure_data["loop_detected"] = True
                            digest = (
                                contract_digest(subtask.user_request)
                                if (subtask.user_request or "").strip()
                                else None
                            )
                            stall = build_stall_signal(
                                verdict,
                                subtask_id=subtask.id,
                                contract_digest=digest,
                            )
                            if stall is not None:
                                stall_payload = stall.to_dict()
                                failure_data["stall_signal"] = stall_payload
                                result.stall_signal = stall_payload
                        if (
                            attempt < self.MAX_RETRIES - 1
                            and result.retryable
                            and not result.cleanup_pending
                            and failure_action.worth_another_call
                            and verdict.action == "retry"
                        ):
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
                            last_failure = result.error or result.summary
                            # The route the next attempt will run at:
                            # recovery stage, budget shrunk on a schema
                            # failure. Same omission rule as
                            # subtask_started -- no table, no field.
                            next_route = self._model_router.route_subtask(
                                subtask.description,
                                attempt=attempt + 1,
                                last_failure=last_failure,
                                file_constraints=subtask.file_constraints,
                                capabilities=subtask.required_capabilities,
                            )
                            if not next_route.is_identity:
                                failure_data["route"] = next_route.to_dict()
                            # Complementary adapter routing (opt-in): the
                            # failed adapter is excluded when the profile store
                            # knows an alternative, so the retry can change
                            # implementation strategy instead of repeating the
                            # same one.  The event carries the decision only —
                            # prompts and profiles stay out of the stream.
                            adapter_choice = self._route_retry_adapter(
                                subtask,
                                last_failure=last_failure,
                                failure_kind=failure_action.action,
                            )
                            if adapter_choice is not None:
                                failure_data["adapter"] = adapter_choice.to_dict()
                            # The recovery view for the attempt about to run:
                            # its prior rounds now include this attempt's
                            # captured patch, so the retry event can report
                            # what the next prompt will carry (counts only).
                            next_view = await self._load_recovery_view(
                                subtask, attempt + 1
                            )
                            recovery_summary = (
                                next_view.summary if next_view is not None else {}
                            )
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
                                    "failure_action": failure_action.action,
                                    # Counts/budget only: what the next attempt's
                                    # recovery context contains (or that it was
                                    # empty), for the cost/benefit A-B evaluation.
                                    **({"recovery": recovery_summary} if recovery_summary else {}),
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
                        if failure_data.get("loop_detected"):
                            # Stop the *outer* dispatch retry too: it would
                            # replay the same evidence and hit the same wall.
                            result.retryable = False
                            result.summary = (
                                (result.summary or "")
                                + " (retry loop stopped: no new evidence for "
                                + f"{failure_data['progress']['stalled_streak']} attempts)"
                            )
                            result.error = (result.error or result.summary)[:2000]
                        await self._record_decision_terminal(subtask, result)
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
                    if needs_worktree:
                        if workspace_handle:
                            await self._workspace_manager.quarantine(workspace_handle)
                        failed = SubtaskResult(
                            subtask_id=subtask.id,
                            worker_id=self.worker_id,
                            success=False,
                            retryable=False,
                            cleanup_pending=True,
                            summary="Inference delivery interrupted; workspace preserved",
                            error=str(e)[:2000],
                        )
                        await self._record_decision_terminal(subtask, failed)
                        return failed
                    # Release workspace without merge on error
                    if workspace_handle:
                        await self._workspace_manager.release(workspace_handle, merge=False)
                        workspace_handle = None  # ponytail: freed; re-acquire next attempt

                    # Retry transient/unknown exceptions, and give a schema
                    # failure its one reduced retry. Permanent provider
                    # auth/configuration errors cannot recover by waiting.
                    exception_action = classify_failure(e)
                    if attempt < self.MAX_RETRIES - 1 and exception_action.worth_another_call:
                        subtask.retry_count += 1
                        last_failure = str(e)
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
                                "failure_action": exception_action.action,
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
                        # ponytail: truncate the event payload's error to [:200],
                        # matching the retry path (line ~872) — the final-failure
                        # branch sent the raw str(e) while the retry branch capped
                        # it. A multi-KB traceback could exceed NATS max_payload;
                        # the SubtaskResult below keeps the full [-2000:] for stderr.
                        data={"error": str(e)[:200], "worker_id": self.worker_id},
                    )
                    friendly_summary, error_field = _build_friendly_error(e)
                    failed = SubtaskResult(
                        subtask_id=subtask.id,
                        worker_id=self.worker_id,
                        summary=friendly_summary,
                        success=False,
                        error=error_field,
                        stderr_tail=str(e)[-2000:],
                        retry_count=subtask.retry_count,
                    )
                    await self._record_decision_terminal(subtask, failed)
                    return failed

            # Should not reach here, but safety net
            # ponytail: unreachable guard — all branches return or continue
            return result or SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary="All retry attempts exhausted",
                success=False,
                retry_count=subtask.retry_count,
            )

        except asyncio.CancelledError:
            if needs_worktree and workspace_handle:
                await self._workspace_manager.quarantine(workspace_handle)
            raise
        finally:
            self.current_task = None
            self._active_count = max(0, self._active_count - 1)

    @staticmethod
    def _attempt_checkpoint_key(subtask: Subtask) -> str:
        """Memory key scoped to ONE attempt (T4 #640).

        The identity triple is the transitional envelope mapping
        ``graph_id = parent_id``, ``node_id = id``, ``attempt_id =
        dispatch_retry_count`` — the same one every ``uc.subtask.execute``
        publisher stamps. Scoping on the subtask alone made a re-dispatched
        attempt inherit the previous attempt's result.
        """
        return f"attempt:{subtask.parent_id}:{subtask.id}:{subtask.dispatch_retry_count}"

    async def _domain_state(self):
        from pathlib import Path

        from ultimate_coders.runtime_state import RuntimeState

        path = Path(
            self._sandbox_config.env_vars.get("UC_RUNTIME_STATE_DIR")
            or os.environ.get("UC_RUNTIME_STATE_DIR")
            or str(Path(self._sandbox_config.project_path or os.getcwd()) / ".uc/runtime")
        )
        return await asyncio.to_thread(
            RuntimeState,
            path / "state.sqlite3",
            database_url=self._sandbox_config.env_vars.get("UC_DATABASE_URL"),
        )

    async def _record_inference_delivery(self, domain: dict, delivery: dict) -> None:
        state = await self._domain_state()
        results = [domain] + [item.get("result", {}) for item in domain.get("steps", [])]
        for item in results:
            if item.get("experiment_id"):
                await asyncio.to_thread(
                    state.mutate,
                    "experiments",
                    item["experiment_id"],
                    lambda old: {**old, "delivery": delivery},
                )

    async def _record_context_manifest(self, subtask: Subtask, summary: dict) -> None:
        """Persist this dispatch attempt's context-compilation summary for audit.

        The key carries the attempt on purpose: the compiler runs once per
        dispatch, so a re-dispatch recompiles and both snapshots must
        survive as history. The attempt is ``dispatch_retry_count`` -- the
        same identity the checkpoint and the terminal-delivery keys use.
        Hardcoding 0 here collapsed every re-dispatch onto attempt 0, so
        ``record()`` replaced the earlier attempt's snapshot instead of
        preserving it and the attempt component was dead in production.

        Best-effort by design: the snapshot answers "why was the prompt
        smaller than expected" after the fact, so losing it must never
        fail the subtask it describes. Both injection paths compile through
        the same compiler and record here: the gateway-composed block used to
        bypass the budget (and the manifest), which left no answer at all for
        prompts assembled from committed graph outputs.
        """
        try:
            from ultimate_coders.context_outbox import ContextManifestOutbox, manifest_key

            state = await self._domain_state()
            await asyncio.to_thread(
                ContextManifestOutbox(state).record,
                manifest_key(subtask.parent_id, subtask.id, subtask.dispatch_retry_count),
                summary=summary,
            )
        except Exception:
            logger.debug("context manifest persistence failed", exc_info=True)

    def _attempt_ref(self, subtask: Subtask, attempt: int) -> AttemptRef:
        """Knowledge-layer identity of the in-worker attempt at ``attempt``.

        ``dispatch_attempt`` is the dispatch counter the checkpoint, the
        context-manifest key and the Decision Record already use, so an
        in-worker retry and a re-dispatch can never collide in the ledger.
        """
        return AttemptRef(
            task_id=subtask.parent_id,
            subtask_id=subtask.id,
            dispatch_attempt=int(getattr(subtask, "dispatch_retry_count", 0) or 0),
            attempt=int(attempt),
            worker_id=self.worker_id,
            # The graph scope makes an attempt's claims eligible for graph-wide
            # sharing inside its own task graph - and never outside it.
            graph_id=subtask.parent_id or "",
        )

    def _verification_report(self, result: SubtaskResult) -> VerificationReport:
        """Extract the verdicts this attempt actually reported.

        Structured carriers only (``SubtaskResult.review`` and domain-result
        blocks); missing data stays unknown rather than being guessed.
        Best-effort: extraction must never fail the attempt it describes.
        """

        try:
            return extract_verification(
                review=result.review, domain_result=result.domain_result
            )
        except Exception:
            logger.debug("verification verdict extraction failed", exc_info=True)
            return VerificationReport()

    def _verdict_evidence(
        self, report: VerificationReport
    ) -> list[tuple[EvidenceKind, dict[str, Any], str]]:
        """Map a verification report onto (kind, payload, summary) evidence rows.

        Only reported verdicts become rows: recording an "unknown" row would
        make a later reader believe a check ran when none did.
        """

        rows: list[tuple[EvidenceKind, dict[str, Any], str]] = []
        if report.review_verdict:
            rows.append(
                (
                    EvidenceKind.REVIEW_VERDICT,
                    {
                        "verdict": report.review_verdict,
                        "issues": list(report.review_issues),
                    },
                    "review verdict",
                )
            )
        if report.oracle_verdict:
            rows.append(
                (
                    EvidenceKind.ORACLE_VERDICT,
                    {
                        "verdict": report.oracle_verdict,
                        "reasons": list(report.oracle_reasons),
                    },
                    "oracle verdict",
                )
            )
        if report.test_verdict:
            rows.append(
                (
                    EvidenceKind.TEST_VERDICT,
                    {
                        "verdict": report.test_verdict,
                        "summary": report.test_summary,
                    },
                    "test verdict",
                )
            )
        return rows

    def _usage_payload(self, result: SubtaskResult) -> dict[str, int]:
        """Reported token usage for the A-B metric, or ``{}`` when unreported.

        A missing number must stay missing (``None`` is "unknown", not zero),
        otherwise the metric would silently treat "not reported" as a
        suspiciously cheap attempt.
        """

        usage = getattr(result, "usage", None)
        payload: dict[str, int] = {}
        if usage is None:
            return payload
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        if isinstance(input_tokens, int):
            payload["input_tokens"] = input_tokens
        if isinstance(output_tokens, int):
            payload["output_tokens"] = output_tokens
        return payload

    async def _record_attempt_round(
        self,
        subtask: Subtask,
        attempt: int,
        result: SubtaskResult,
        workspace_handle: WorkspaceHandle | None,
    ) -> None:
        """Append this attempt's patch round to the diff-chain ledger.

        Captured while the worktree is still allocated (before ``release``):
        the candidate diff is the only durable record of what an attempt
        actually changed, and a failed attempt's patch is the primary input of
        the next retry's recovery view. Round content is bounded by the rollout
        gates (``UC_KNOWLEDGE_MAX_PATCH_FILES`` / ``UC_KNOWLEDGE_MAX_DIFF_CHARS``)
        and symbol-located AST-first, with line-level fallback.

        Best-effort by design: evidence capture is audit, not delivery. A
        ledger failure must never fail (or slow) the subtask it describes, so
        errors are logged at debug and swallowed. Stage-disabled (the default,
        ``UC_KNOWLEDGE_STAGE`` unset) returns immediately: Stage A keeps the
        legacy execution path byte-identical.
        """
        stage = rollout.stage_from_env()
        if not rollout.capture_enabled(stage) or self._workspace_manager is None:
            return
        if workspace_handle is None:
            return
        try:
            snapshot = await self._workspace_manager.capture_diff(
                workspace_handle,
                max_files=rollout.max_patch_files(),
                max_diff_chars=rollout.max_patch_diff_chars(),
            )
            patches: list[dict[str, Any]] = []
            for item in snapshot.get("files") or ():
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                diff = str(item.get("diff") or "")
                patches.append(
                    {
                        "path": path,
                        "change_type": _DIFF_CHANGE_TYPES.get(
                            str(item.get("change_type") or "").lower(), "modified"
                        ),
                        "diff": diff,
                        # One snapshot-wide flag: a file whose body was clipped
                        # must not read as a complete patch.
                        "truncated": bool(snapshot.get("truncated")),
                        "symbols": [
                            symbol.to_dict()
                            for symbol in locate_symbols(
                                path,
                                diff,
                                source_text=self._worktree_source(workspace_handle, path),
                            )
                        ],
                    }
                )
            record = AttemptRound(
                attempt=self._attempt_ref(subtask, attempt),
                outcome=(
                    "completed"
                    if result.success
                    else ("cleanup_pending" if result.cleanup_pending else "failed")
                ),
                patches=tuple(patches),
                base_revision=str(snapshot.get("baseline") or ""),
                head_revision=str(snapshot.get("head") or ""),
                summary=result.summary or "",
                failure=result.error or "",
                # Verified verdicts only: the round label is derived from
                # structured review/oracle/test carriers, so a round without
                # evidence still reads "unknown" (principle A).
                test_verdict=self._verification_report(result).round_verdict(),
            )
            state = await self._domain_state()
            await asyncio.to_thread(AttemptChainLedger(state).record, record)
        except Exception:
            logger.debug("attempt patch round persistence failed", exc_info=True)

    def _worktree_source(self, handle: WorkspaceHandle, path: str) -> str:
        """Post-change source text of one file for AST-first symbol location."""
        worktree = getattr(handle, "worktree_path", "") or ""
        if not worktree or not path:
            return ""
        full = os.path.join(worktree, path)
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > 500_000:
                return ""
            with open(full, encoding="utf-8", errors="replace") as source:
                return source.read()
        except OSError:
            return ""

    async def _record_attempt_evidence(
        self, subtask: Subtask, attempt: int, result: SubtaskResult
    ) -> None:
        """Record what this attempt proved as bounded ATTEMPT-visibility evidence.

        Kept separate from the patch round by design: the round says what the
        candidate changed (git-observed), this says what the attempt concluded
        (worker-reported). Neither stands in for the other -- a failed
        attempt's error text is a failure signature, never an oracle verdict
        (principle A), and an ATTEMPT record is never shared outside its retry
        chain (the Epistemic Gate owns promotion, with validation and a
        successful attempt).

        Rollout-gated and best-effort: capture is audit, not delivery.
        """
        stage = rollout.stage_from_env()
        if not rollout.capture_enabled(stage):
            return
        try:
            ref = self._attempt_ref(subtask, attempt)
            state = await self._domain_state()
            ledger = EvidenceLedger(state, stage=stage)
            outcome = await asyncio.to_thread(
                ledger.record_evidence,
                attempt=ref,
                kind=EvidenceKind.ATTEMPT_OUTCOME,
                payload={
                    "success": bool(result.success),
                    "retryable": bool(result.retryable),
                    "cleanup_pending": bool(result.cleanup_pending),
                    "attempt": int(attempt),
                    "retry_count": int(result.retry_count or 0),
                    "changed_files": len(result.modified_files or ()),
                    # Reported usage only; a missing number stays missing so the
                    # A-B metric can say "not reported" instead of inventing 0.
                    "usage": self._usage_payload(result),
                },
                summary=result.summary or "",
            )
            evidence_ids = [outcome.evidence_id]
            for kind, payload, summary in self._verdict_evidence(
                self._verification_report(result)
            ):
                record = await asyncio.to_thread(
                    ledger.record_evidence,
                    attempt=ref,
                    kind=kind,
                    payload=payload,
                    summary=summary,
                )
                evidence_ids.append(record.evidence_id)
            if not result.success:
                raw_failure = result.error or result.summary or ""
                signature = normalize_failure_signature(raw_failure)
                failure = await asyncio.to_thread(
                    ledger.record_evidence,
                    attempt=ref,
                    kind=EvidenceKind.FAILURE_SIGNATURE,
                    payload={
                        "signature": signature,
                        # Raw excerpt stays on the evidence for auditing; the
                        # normalized signature is the comparison key.
                        "excerpt": safe_text(raw_failure, limit=500),
                    },
                    summary="attempt failed",
                )
                evidence_ids.append(failure.evidence_id)
                # Negative experience is a claim, not a fact: the next retry
                # (and only its gate-controlled readers) may use it.
                await asyncio.to_thread(
                    record_failure_claim,
                    ledger,
                    attempt=ref,
                    scope=attempt_scope(ref),
                    evidence_ids=evidence_ids,
                    signature=signature,
                )
        except Exception:
            logger.debug("attempt evidence persistence failed", exc_info=True)

    async def _record_delivery_evidence(
        self, subtask: Subtask, attempt: int, merge_result: dict[str, Any]
    ) -> list[str]:
        """Bind the attempt's integration facts after the workspace release.

        A merge receipt is what the *target* accepted (commit sha, branch
        preservation, conflict status); a push verdict is what the *remote*
        said. Recording them as evidence -- instead of folding them into the
        round's ``test_verdict`` -- keeps the "validated vs released" split
        visible: a later promotion can require an integration receipt rather
        than trusting the worker's own success claim (principle B,
        ``push_status == UNKNOWN`` never promotes).

        Returns the recorded evidence ids so the delivery claim can link the
        exact receipt it was validated with (empty when capture is disabled or
        the write failed).
        """
        stage = rollout.stage_from_env()
        if not rollout.capture_enabled(stage):
            return []
        try:
            ref = self._attempt_ref(subtask, attempt)
            state = await self._domain_state()
            ledger = EvidenceLedger(state, stage=stage)
            receipt = await asyncio.to_thread(
                ledger.record_evidence,
                attempt=ref,
                kind=EvidenceKind.MERGE_RECEIPT,
                payload={
                    "status": str(merge_result.get("status") or ""),
                    "commit_sha": str(merge_result.get("commit_sha") or ""),
                    "branch_preserved": str(merge_result.get("branch_preserved") or ""),
                },
                summary="workspace release",
            )
            evidence_ids = [receipt.evidence_id]
            push_status = merge_result.get("push_status")
            if push_status:
                push = await asyncio.to_thread(
                    ledger.record_evidence,
                    attempt=ref,
                    kind=EvidenceKind.PUSH_VERDICT,
                    payload={"push_status": str(push_status)},
                    summary="branch push",
                )
                evidence_ids.append(push.evidence_id)
            return evidence_ids
        except Exception:
            logger.debug("delivery evidence persistence failed", exc_info=True)
            return []

    async def _record_delivery_claim(
        self,
        subtask: Subtask,
        attempt: int,
        result: SubtaskResult,
        merge_result: dict[str, Any],
        evidence_ids: list[str],
    ) -> None:
        """Record the delivered claim and (at stage >= graph) promote it.

        The worker is the claim *producer*; the gate is the only thing that can
        widen its audience. Promotion requires a landed revision so the gate's
        validation/revision binding is honest - without a commit sha the claim
        is recorded at ATTEMPT visibility and left alone.
        """

        stage = rollout.stage_from_env()
        if not rollout.capture_enabled(stage):
            return
        status = str(merge_result.get("status") or "")
        if status not in ACCEPTED_MERGE_STATUSES:
            return
        try:
            ref = self._attempt_ref(subtask, attempt)
            state = await self._domain_state()
            ledger = EvidenceLedger(state, stage=stage)
            scope = attempt_scope(ref)
            revision = str(merge_result.get("commit_sha") or "")
            report = self._verification_report(result)
            claim = await asyncio.to_thread(
                record_delivery_claim,
                ledger,
                attempt=ref,
                scope=scope,
                revision=revision,
                evidence_ids=evidence_ids,
                merge_status=status,
                file_count=len(result.modified_files or ()),
                verification_label=report.round_verdict(),
            )
            if not rollout.graph_sharing_enabled(stage) or not revision:
                return
            if not scope.graph_id:
                return
            _claim, decision = await asyncio.to_thread(
                validate_and_promote_delivery,
                ledger,
                claim.claim_id,
                revision=revision,
                evidence_ids=evidence_ids,
                push_status=str(merge_result.get("push_status") or ""),
                # Never assume success: a failed attempt must not promote.
                attempt_succeeded=bool(result.success),
            )
            logger.debug(
                "knowledge delivery claim %s promoted=%s reasons=%s",
                claim.claim_id,
                decision.allowed,
                list(decision.reasons),
            )
        except Exception:
            logger.debug("delivery claim persistence failed", exc_info=True)

    async def _knowledge_context_items(
        self, subtask: Subtask
    ) -> tuple[list[Any], dict[str, Any]]:
        """Gate-approved graph knowledge for this attempt's context (P5).

        Reads claims the Epistemic Gate already promoted inside this subtask's
        own task graph and packs them within the retrieval budget. Below stage
        ``graph`` this returns ``([], {})``: the read face is opt-in, and its
        summary carries counts/tokens only - never claim statements.
        """

        stage = rollout.stage_from_env()
        if not rollout.graph_sharing_enabled(stage):
            return [], {}
        if not subtask.parent_id:
            return [], {}
        try:
            state = await self._domain_state()
            ledger = EvidenceLedger(state, stage=stage)
            scope = KnowledgeScope(graph_id=subtask.parent_id)
            return await asyncio.to_thread(
                knowledge_context_items, ledger, scope=scope
            )
        except Exception:
            logger.debug("graph knowledge read failed", exc_info=True)
            return [], {}

    async def _load_recovery_view(
        self, subtask: Subtask, attempt: int
    ) -> RecoveryView | None:
        """Read the prior rounds of this subtask and build its recovery view.

        ``None`` means "no recovery evidence": the rollout stage is below
        recovery, the attempt is the first one, the ledger holds no strictly
        earlier round, or the read/build failed. Callers treat it as an empty
        recovery (keep the base prompt, omit the event field), so Stage A/B
        retries stay byte-identical to the pre-upgrade loop.
        """
        stage = rollout.stage_from_env()
        if not rollout.recovery_enabled(stage) or attempt <= 0:
            return None
        try:
            state = await self._domain_state()
            current = self._attempt_ref(subtask, attempt)
            prior = await asyncio.to_thread(
                lambda: AttemptChainLedger(state).prior_rounds(
                    current, limit=rollout.chain_max_rounds()
                )
            )
            if not prior:
                return None
            return build_recovery_view(
                attempt=current, rounds=[record.to_dict() for record in prior]
            )
        except Exception:
            logger.debug("recovery chain read failed", exc_info=True)
            return None

    async def _recovery_context_block(
        self,
        subtask: Subtask,
        attempt: int,
        base_items: list,
        current_block: str,
        *,
        legacy_style: str,
        gateway_truncated: bool,
    ) -> tuple[str, dict[str, Any]]:
        """Recompile the retry attempt's context with prior-attempt evidence.

        Returns ``(context_block, recovery_summary)``. When the rollout stage
        is below recovery, or the ledger holds no prior round, the first
        attempt's prompt is returned unchanged with an empty summary.

        Recompiling (rather than appending a rendered section) keeps one budget
        across base context and recovery evidence: on a high-risk attempt the
        P2 reservation now protects the recovery facts, and an over-tight
        budget surfaces as a manifest drop instead of a prompt that grew past
        the budget. The manifest summary is refreshed for the attempt's event
        but not re-recorded under the same dispatch key.
        """
        view = await self._load_recovery_view(subtask, attempt)
        if view is None:
            return current_block, {}
        try:
            items = recovery_context_items(view)
        except Exception:
            logger.debug("recovery view render failed", exc_info=True)
            return current_block, {}
        if not items:
            return current_block, view.summary
        summary = dict(view.summary)
        # AST-anchored attempt evolution (opt-in): a compact, symbol-level
        # ``old -> new`` chain is often more useful on a retry than replaying
        # whole diffs, and it lets a rescued attempt see what was already
        # changed and reverted.  It is rendered from the same bounded rounds
        # the recovery view already selected.
        try:
            from ultimate_coders.architecture.evolution import (
                compact_evolution,
                evolution_enabled,
            )

            if evolution_enabled() and view.rounds:
                chain = compact_evolution(
                    view.rounds,
                    task_id=subtask.parent_id,
                    subtask_id=subtask.id,
                )
                rendered = chain.render(max_changes=12)
                if rendered:
                    from ultimate_coders.agent.context_compiler import (
                        ContextItem,
                        Tier,
                    )

                    items.append(
                        ContextItem(
                            item_id=f"attempt-evolution:{subtask.id}:{attempt}",
                            tier=Tier.EVIDENCE,
                            content="Attempt evolution (old -> new):\n" + rendered,
                            source="attempt-evolution",
                            relevance=0.85,
                            stable=True,
                        )
                    )
                    summary["evolution_changes"] = len(chain.changes)
                    summary["evolution_reverted_rounds"] = len(chain.reverted_rounds)
        except Exception:
            logger.debug("attempt evolution render failed", exc_info=True)
        manifest = compile_context(
            list(base_items) + items,
            budget_tokens=self._context_budget_tokens(),
            risk=self._last_route.risk,
        )
        self._last_context_manifest = manifest.summary()
        return (
            manifest.render_legacy(
                legacy_style=legacy_style, gateway_truncated=gateway_truncated
            ),
            summary,
        )

    async def _record_decision_start(self, subtask: Subtask) -> None:
        """Persist this dispatch attempt's Decision Record start facts.

        Joins worker identity, optional non-identity route, and a pointer at
        the context-manifest summary (digest + key) — never prompt content.
        Called after route + context compile (and any routed agent_config) and
        before workspace acquire, so allocation failures still leave a start
        row. Attempt identity is ``dispatch_retry_count``. Best-effort:
        persistence failure must never fail the subtask.
        """
        try:
            from ultimate_coders.context_outbox import manifest_key
            from ultimate_coders.decision_record import (
                DecisionRecordOutbox,
                context_digest,
                decision_key,
            )

            route = None
            if self._last_route is not None and not self._last_route.is_identity:
                route = self._last_route.to_dict()
            digest = None
            ctx_key = None
            if self._last_context_manifest is not None:
                digest = context_digest(self._last_context_manifest)
                ctx_key = manifest_key(
                    subtask.parent_id, subtask.id, subtask.dispatch_retry_count
                )
            governance = subtask.agent_config.get("governance")
            if governance is not None and not isinstance(governance, dict):
                governance = None

            state = await self._domain_state()
            await asyncio.to_thread(
                DecisionRecordOutbox(state).record_start,
                decision_key(subtask.parent_id, subtask.id, subtask.dispatch_retry_count),
                worker_id=self.worker_id,
                route=route,
                context_digest=digest,
                context_key=ctx_key,
                governance=governance,
            )
        except Exception:
            logger.debug("decision record start persistence failed", exc_info=True)

    async def _record_decision_terminal(
        self, subtask: Subtask, result: SubtaskResult
    ) -> None:
        """Merge terminal facts into this dispatch attempt's Decision Record.

        Called only on paths that finish the attempt (success or final
        failure), not on retry-continue. Compact summary / error class only;
        optional oracle/review when the domain result or typed review carries
        them. Best-effort like the start write.
        """
        try:
            from ultimate_coders.decision_record import (
                DecisionRecordOutbox,
                decision_key,
                workspace_summary,
            )

            domain = result.domain_result if isinstance(result.domain_result, dict) else {}
            oracle = domain.get("oracle") if isinstance(domain.get("oracle"), dict) else None
            review = None
            if result.review is not None:
                review = result.review.to_dict()
            elif isinstance(domain.get("review"), dict):
                review = domain["review"]
            summary = (result.summary or "")[:300] or None
            error_class = None
            if not result.success:
                error_class = classify_failure(result.error or result.summary).kind

            state = await self._domain_state()
            await asyncio.to_thread(
                DecisionRecordOutbox(state).record_terminal,
                decision_key(subtask.parent_id, subtask.id, subtask.dispatch_retry_count),
                success=result.success,
                summary=summary,
                error_class=error_class,
                oracle=oracle,
                review=review,
                workspace=workspace_summary(result.modified_files, result.recent_tool_calls),
            )
        except Exception:
            logger.debug("decision record terminal persistence failed", exc_info=True)

    async def confirmed_effect_ids_for(self, task, subtask, result) -> list[str]:
        """Confirmed effects this attempt owns, for the watchdog's preserve_effects.

        Scope is read off what the ledger records already carry (D18): the
        subtask's branch in both its coding and inference forms, and the
        attempt's experiment-id prefix on MetaInfer ``operation_id``s. Effects
        that cannot be scoped stay out — the watchdog then protects nothing
        rather than a guessed id.
        """
        try:
            from ultimate_coders.agent.workspace import subtask_branch
            from ultimate_coders.effect_ledger import EffectLedger

            branches = (
                subtask_branch(subtask.id),
                subtask_branch(subtask.id, inference=True),
            )
            domain = result.domain_result if isinstance(result.domain_result, dict) else {}
            experiment = domain.get("experiment_id")
            prefix = f"{experiment}:" if isinstance(experiment, str) and experiment else None
            ledger = EffectLedger(await self._domain_state())
            return await asyncio.to_thread(
                ledger.confirmed_effect_ids,
                branches=branches,
                operation_prefix=prefix,
            )
        except Exception:  # noqa: BLE001 - evidence must never fail the subtask
            logger.debug("confirmed-effect lookup failed", exc_info=True)
            return []

    @staticmethod
    def _is_inference(subtask: Subtask) -> bool:
        return bool(
            subtask.agent_config.get("inference_task")
            or any(step.agent in ("metainfer", "inference-infra") for step in subtask.steps)
        )

    async def _save_checkpoint(
        self, subtask: Subtask, result: SubtaskResult, *, delivery="complete"
    ) -> None:
        """Persist this attempt's result to engine memory for resume.

        Stores full result including modified_files, tool_calls, and error
        so that resume can reconstruct a complete SubtaskResult.

        ponytail: F59 — async (was sync): in gRPC mode write_memory blocked
        the event loop for up to 30s. Non-fatal on failure.
        """
        data: dict[str, Any] = {
            "subtask_id": subtask.id,
            "attempt_id": subtask.dispatch_retry_count,
            "worker_id": result.worker_id,
            "summary": result.summary,
            "success": result.success,
            "retryable": result.retryable,
            "cleanup_pending": result.cleanup_pending,
            "domain_result": result.domain_result,
            "delivery": delivery,
            "modified_files": [
                {"file_path": f.file_path, "change_type": f.change_type.value}
                for f in (result.modified_files or [])
            ],
            "tool_calls": result.recent_tool_calls[-5:] if result.recent_tool_calls else [],
            "error": result.summary if not result.success else None,
            "stderr_tail": result.stderr_tail,
        }
        if self._is_inference(subtask):
            state = await self._domain_state()
            await asyncio.to_thread(
                state.mutate,
                "attempt_results",
                self._attempt_checkpoint_key(subtask),
                lambda _: data,
            )
        if self.engine is None:
            return
        try:
            if result.review is not None:
                data["review"] = result.review.to_dict()
            await _engine_call(
                self.engine,
                "write_memory",
                "write_memory_async",
                key_scope="checkpoint",
                key=self._attempt_checkpoint_key(subtask),
                content=json.dumps(data),
                content_type="structured",
                source_agent="worker",
            )
        except Exception:
            logger.debug("Failed to save checkpoint for subtask %s", subtask.id[:8])

    async def _load_checkpoint(self, subtask: Subtask) -> dict | None:
        """Load THIS attempt's checkpoint (F59: async, see _save_checkpoint).

        Returns ``None`` for a fresh attempt — that is the whole point of the
        T4 fix: re-dispatch after a fence must execute, not replay the
        previous attempt's result.
        """
        if self._is_inference(subtask):
            state = await self._domain_state()
            persisted = await asyncio.to_thread(
                state.get, "attempt_results", self._attempt_checkpoint_key(subtask)
            )
            if persisted:
                if persisted.get("delivery") == "pending":
                    leases = await asyncio.to_thread(state.records, "workspace_leases")
                    delivery = next(
                        (
                            lease.get("delivery")
                            for lease in leases
                            if lease.get("handle", {}).get("subtask_id") == subtask.id
                            and lease.get("status") in ("completed", "delivered")
                        ),
                        None,
                    )
                    if delivery:
                        persisted["delivery"] = "complete"
                        if persisted.get("domain_result"):
                            persisted["domain_result"]["delivery"] = delivery
                        await asyncio.to_thread(
                            state.mutate,
                            "attempt_results",
                            self._attempt_checkpoint_key(subtask),
                            lambda _: persisted,
                        )
                return persisted
        if self.engine is None:
            return None
        try:
            raw = await _engine_call(
                self.engine,
                "read_memory",
                "read_memory_async",
                key_scope="checkpoint",
                key=self._attempt_checkpoint_key(subtask),
            )
            if raw is not None:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            logger.debug("Failed to load checkpoint for subtask %s", subtask.id[:8])
        return None

    async def _execute_in_sandbox(
        self,
        subtask: Subtask,
        context_block: str = "",
        workspace_handle: Any | None = None,
    ) -> SubtaskResult:
        """Execute via sandbox (Grok Build by default; Claude Code / Codex optional).

        Automatically declares and releases EditIntent for file_constraints
        so the conflict detector tracks which files are being modified.

        If context_block is provided, prepends it to the subtask prompt.
        If workspace_handle is provided, executes in the workspace directory.
        """
        # Match the gateway's exact capability predicate; prose containing
        # "review" must not silently opt a normal task into a new contract.
        is_review = "review" in subtask.required_capabilities
        if is_review:
            context_block = f"{context_block}\n\n{REVIEW_INSTRUCTIONS}".strip()

        # Optional narrowing Mandate envelope. It is checked before the
        # worktree/conflict machinery so an expired or over-broad attempt does
        # not acquire resources it is not authorized to use.
        mandate_event, mandate_error = self._apply_mandate(subtask)
        if mandate_event is not None:
            await self._emit_step_event(subtask, "subtask_progress", **mandate_event)
        if mandate_error:
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                summary="Mandate envelope rejected",
                success=False,
                retryable=False,
                error=mandate_error,
            )

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

            # Optional zero-edit PerceptionPass: compile a grounded
            # implementation spec from the worktree before the agent starts.
            # It is read-only and flag-gated; failure to compile must never
            # block the existing prompt path.
            context_block = self._apply_perception_pass(subtask, context_block, working_dir)

            # Optional ExecCritic acceptance contract. Validation happens
            # before any source edit; an enforced invalid artifact fails the
            # subtask without launching the agent.
            critic_event, critic_error = self._exec_critic_preflight(subtask)
            if critic_event is not None:
                await self._emit_step_event(subtask, "subtask_progress", **critic_event)
            if critic_error:
                return SubtaskResult(
                    subtask_id=subtask.id,
                    worker_id=self.worker_id,
                    summary="ExecCritic rejected the issued test artifact",
                    success=False,
                    retryable=False,
                    error=critic_error,
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

            # Multi-agent workflow: run ordered steps, threading each step's
            # output into the next step's prompt template. Empty steps = the
            # legacy single-agent path (backward compatible).
            # T7 #643 — cancel_key registers the spawned agent process group
            # under (task_id, node_id) so control events can kill it.
            cancel_key = (subtask.parent_id, subtask.id)
            if subtask.steps:
                output = await self._execute_steps(
                    subtask,
                    working_dir,
                    _on_stdout_line,
                    context_block,
                    cancel_key=cancel_key,
                )
            else:
                # Build prompt with context injection
                description = subtask.description
                if context_block:
                    description = f"{context_block}\n\n{description}"

                prompt = _SUBTASK_USER_TEMPLATE.format(
                    description=description,
                    user_request=subtask.user_request or subtask.description,
                    expected_output=subtask.expected_output or "Complete the described task",
                    file_constraints=", ".join(subtask.file_constraints) or "none",
                )

                from ultimate_coders.inference.agent import InferenceInfraAgent

                agent_config = self._resolve_agent_config(subtask) or {}
                routed = InferenceInfraAgent.route(subtask.description, agent_config)
                if routed:
                    agent_config = routed
                    agent_config = {
                        **agent_config,
                        "uc_execution": {
                            "graph_id": subtask.parent_id,
                            "node_id": subtask.id,
                            "attempt_id": subtask.dispatch_retry_count,
                            "step_index": 0,
                        },
                    }
                    state = await self._domain_state()
                    agent_config["_uc_runtime_state_dir"] = str(state.path.parent)
                    if self._workspace_manager:
                        agent_config[
                            "_uc_workspace_owner"
                        ] = await self._workspace_manager.runner_owner(working_dir)

                sandbox_config = getattr(self, "_sandbox_config", None)
                subtask._last_agent = (  # type: ignore[attr-defined]
                    agent_config.get("agent")
                    or getattr(sandbox_config, "agent", "")
                    or ""
                )
                if str(subtask._last_agent) in ("local-harness", "local-llm"):
                    agent_config.update(self._drift_hints(subtask))
                    agent_config.update(self._radio_hints(subtask))
                output: AgentOutput = await self._sandbox_manager.execute(
                    prompt,
                    working_dir=working_dir,
                    on_stdout_line=_on_stdout_line,
                    subtask_config=agent_config or None,
                    agent=agent_config.get("agent"),
                    cancel_key=cancel_key,
                )
            # TRACE (#20) consumes one real answer likelihood per model turn.
            # Only adapters that requested token logprobs populate this list;
            # the event is omitted entirely when the provider reported none.
            if output.turn_likelihoods:
                await self._emit_step_event(
                    subtask,
                    "subtask_progress",
                    phase="trace likelihoods",
                    percent=0,
                    turn_likelihoods=list(output.turn_likelihoods)[:64],
                )
            diagnosis = self._runtime_diagnosis(output)
            if diagnosis is not None:
                await self._emit_step_event(subtask, "subtask_progress", **diagnosis)
            # T18 #668: the workflow path collected one record per executed step
            # as it went; the legacy single-agent path declares no step yet still
            # executed exactly one unit. Record that unit at index 0, so a
            # single-step subtask's terminal event is as readable as a chain's
            # first step (`payload.steps[]` length 1) and no consumer has to
            # special-case the majority path.
            #
            # Branching on `subtask.steps` rather than on "the collected list is
            # empty" is load-bearing: a workflow whose every step was skipped by
            # its condition legitimately collects nothing (nothing ran), and
            # synthesising an entry there would invent a unit that never ran.
            #
            # The empty `agent` is deliberate: the legacy form names no adapter,
            # so `source` falls back to the stamp the adapter put on its own
            # usage block, and stays `null` when it reported nothing at all.
            # That is the honest answer — there is no declared step to name.
            step_usages = output.step_usages if subtask.steps else [_step_usage(0, "", "", output)]
            # ponytail: extract stderr_tail and recent tool calls for failure context
            stderr_tail = output.stderr_tail
            if not stderr_tail and hasattr(output, "raw_stderr") and output.raw_stderr:
                stderr_tail = "\n".join(output.raw_stderr.strip().splitlines()[-10:])
            # T12 #654 / D12 #649: feed the affinity summary. Both what the
            # node DECLARED and what it actually CHANGED count as evidence of
            # where this worker's expertise lies — the declared set is often
            # the only signal for a read-only node.
            self.record_recent_files(
                list(subtask.file_constraints) + [fc.file_path for fc in output.file_changes]
            )
            review = parse_review(output.summary) if is_review else None
            success = output.success
            error = "" if output.success else output.summary[:2000]
            if is_review:
                if not output.success:
                    review = None
                elif output.file_changes or output.failed_file_changes:
                    success, error, review = False, "Review modified files", None
                elif output.had_failed_step:
                    success, error, review = False, "Review workflow contained a failed step", None
                elif review is None:
                    success, error = False, "Review returned no valid JSON verdict"
                elif not review.approved:
                    success, error = False, "Review rejected: " + "; ".join(review.issues)
            if success and output.domain_result is not None:
                await self.write_shared_memory(
                    key=f"inference:{subtask.parent_id}:{subtask.id}",
                    content=json.dumps(output.domain_result, ensure_ascii=False),
                    project_id=subtask.project_id,
                    content_type="structured",
                    tags=["inference_infra", "benchmark", "adaptation_graph"],
                )
            if success:
                process_report = await self._verify_output_process(working_dir, output)
                if process_report is not None and not process_report.passed:
                    details = "; ".join(
                        f"{finding.kind}:{finding.path}:{finding.line}"
                        for finding in process_report.errors[:5]
                    )
                    success = False
                    error = f"Process review rejected: {details}"[:2000]
            if success:
                anti_report = await self._verify_output_anti_patterns(working_dir, output)
                if anti_report is not None and not anti_report.passed:
                    details = "; ".join(
                        f"{finding.kind}:{finding.path}:{finding.line}"
                        for finding in anti_report.errors[:5]
                    )
                    success = False
                    error = f"Anti-pattern gate rejected: {details}"[:2000]
            dockerless = await self._verify_output_dockerless(working_dir, output)
            if dockerless is not None:
                await self._emit_step_event(
                    subtask,
                    "subtask_progress",
                    phase="dockerless verification",
                    percent=0,
                    dockerless_score=dockerless.score,
                    dockerless_passed=dockerless.passed,
                    dockerless_missing=list(dockerless.missing_evidence)[:8],
                )
            retrace = await self._verify_output_retrace(subtask, working_dir, output)
            if retrace is not None:
                await self._emit_step_event(
                    subtask,
                    "subtask_progress",
                    phase="retrace verification",
                    percent=0,
                    retrace_status=retrace.status,
                    retrace_passed=retrace.passed,
                    retrace_missing=list(retrace.missing)[:8],
                    retrace_contradictory=list(retrace.contradictory)[:8],
                    retrace_repair=retrace.repair_instruction[:500],
                )
            critic_event, critic_error = await self._verify_output_exec_critic(
                subtask,
                working_dir,
                output,
            )
            if critic_event is not None:
                await self._emit_step_event(subtask, "subtask_progress", **critic_event)
            if critic_error:
                success = False
                error = critic_error
            boundary_event, boundary_error = self._verify_output_boundary(
                subtask,
                output,
            )
            if boundary_event is not None:
                await self._emit_step_event(subtask, "subtask_progress", **boundary_event)
            if success and boundary_error:
                success = False
                error = boundary_error
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                modified_files=output.file_changes,
                summary=output.summary,
                success=success,
                retryable=output.retryable,
                cleanup_pending=output.cleanup_pending,
                domain_result=output.domain_result,
                error=error[:2000],
                review=review,
                stderr_tail=stderr_tail,
                recent_tool_calls=output.tool_calls[-5:],
                # T15 #660 / D13 #657: the adapters have parsed token/cost out
                # of agent stdout since T5, but this constructor dropped it on
                # the floor while passing every other AgentOutput field — one of
                # the two hops that kept execution_events.cost/tokens NULL.
                # `None` when the adapter reported nothing (stays NULL, never 0).
                usage=subtask_usage_from_token_usage(output.token_usage),
                # T18 #668: the per-step disaggregation of the block above. It
                # rides the same constructor for the same reason T15 gave for
                # `usage` — every other AgentOutput field is forwarded here, and
                # a field dropped at this hop is exactly how the original
                # under-report survived: nothing errors, the number is simply
                # gone by the time the terminal event is written.
                step_usages=step_usages,
            )
        finally:
            # Release edit intent after execution (success or failure)
            for fp in declared_files:
                self.conflict_detector.remove_intent(fp, self.worker_id)

    async def _emit_step_event(self, subtask: Subtask, event_type: str, **data: Any) -> None:
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

    def _apply_perception_pass(
        self,
        subtask: Subtask,
        context_block: str,
        working_dir: str | None,
    ) -> str:
        """Append a grounded ``ImplementationSpec`` to the prompt context.

        Opt-in via ``UC_PERCEPTION_PASS``. The compiler is read-only and never
        invokes a model; any failure leaves the original context untouched.
        """

        from ultimate_coders.agent.boundary_contract import (
            BoundaryContract,
            boundary_contract_enabled,
        )
        from ultimate_coders.agent.planner import PerceptionPass, perception_pass_enabled

        perception_enabled = perception_pass_enabled()
        boundary_enabled = boundary_contract_enabled()
        if not (perception_enabled or boundary_enabled) or not subtask.description.strip():
            return context_block
        try:
            spec = PerceptionPass().compile(
                subtask.parent_id or subtask.id,
                subtask.description,
                repo_root=working_dir or ".",
                file_constraints=subtask.file_constraints,
            )
        except Exception:
            logger.debug("perception pass failed for %s", subtask.id[:8], exc_info=True)
            return context_block

        if boundary_enabled:
            try:
                subtask._boundary_contract = BoundaryContract.from_implementation_spec(
                    spec,
                    file_constraints=subtask.file_constraints,
                )
            except Exception:
                logger.debug(
                    "boundary contract build failed for %s",
                    subtask.id[:8],
                    exc_info=True,
                )
        if not perception_enabled:
            return context_block

        lines = [
            "## Implementation spec (PerceptionPass)",
            f"Confidence: {spec.confidence:.2f}",
        ]
        if spec.target_files:
            lines.append("### Target boundaries")
            for boundary in spec.target_files[:20]:
                suffix = f" — {boundary.reason}" if boundary.reason else ""
                lines.append(f"- {boundary.role}: `{boundary.path}`{suffix}")
        if spec.public_api:
            lines.append("### Public API to preserve")
            lines.extend(f"- `{item}`" for item in spec.public_api[:12])
        if spec.dependency_closure:
            lines.append("### Read-only dependency closure")
            lines.extend(f"- `{path}`" for path in spec.dependency_closure[:16])
        if spec.checks:
            passed = sum(1 for check in spec.checks if check.get("status") == "pass")
            lines.append(f"### Static mapping checks: {passed}/{len(spec.checks)} pass")
            for check in spec.checks:
                if check.get("status") != "pass":
                    detail = check.get("detail") or check.get("message") or ""
                    lines.append(
                        f"- {check.get('name', 'check')}: {check.get('status', 'unknown')}"
                        + (f" — {detail}" if detail else "")
                    )
        if spec.target_files:
            from ultimate_coders.agent.planner_critic import (
                PlanCritic,
                critic_prompt,
                planner_critic_enabled,
            )

            if planner_critic_enabled():
                root = Path(working_dir or ".")
                observations = [
                    {
                        "target": boundary.path,
                        "evidence": (root / boundary.path).is_file(),
                    }
                    for boundary in spec.target_files
                ]
                critique = PlanCritic().review(
                    [boundary.path for boundary in spec.target_files],
                    observations,
                )
                status = "approved" if critique.approved else "not approved"
                lines.append(
                    "### Plan critic: "
                    f"{status} (precision={critique.exploration_precision:.2f}, "
                    f"recall={critique.exploration_recall:.2f}, "
                    f"confidence={critique.confidence:.2f})"
                )
                if not critique.approved:
                    lines.append(critic_prompt(critique))
        rendered = "\n".join(lines)[:6000]
        return f"{context_block}\n\n{rendered}".strip()

    def _apply_mandate(self, subtask: Subtask) -> tuple[dict[str, Any] | None, str | None]:
        """Consume an optional narrowing mandate before the attempt starts.

        ``UC_MANDATE_ENVELOPE`` gates the caller, not the dataclass. The task
        carries the envelope in ``agent_config["mandate"]``; ``"enforce": true``
        turns a violation into a non-retryable failure. A malformed or empty
        envelope is always rejected: silently ignoring it would widen authority.

        This is deliberately a consumer of the existing governance mandate, not
        a second mandate store. The Python envelope mirrors only the dimensions
        the worker can check before a sandbox starts: expiry, subject, required
        capabilities, declared resources and attempt ceiling.
        """

        if not mandate_envelope_enabled():
            return None, None
        config = subtask.agent_config if isinstance(subtask.agent_config, dict) else {}
        raw = config.get("mandate")
        if not isinstance(raw, dict):
            return None, None
        enforce = bool(raw.get("enforce", False))
        try:
            mandate = Mandate.from_dict(raw)
        except (MandateError, TypeError, ValueError) as exc:
            return (
                {
                    "phase": "mandate envelope",
                    "percent": 0,
                    "mandate_enforced": enforce,
                    "mandate_error": str(exc)[:300],
                },
                f"Mandate envelope rejected: {exc}",
            )

        violations: list[str] = []
        if not mandate.mandate_id:
            violations.append("mandate_id is empty")
        if not mandate.subject:
            violations.append("subject is empty")
        allowed_subjects = {
            str(value).strip()
            for value in (subtask.parent_id, subtask.id)
            if str(value).strip()
        }
        if mandate.subject and allowed_subjects and mandate.subject not in allowed_subjects:
            violations.append(f"subject {mandate.subject!r} does not match this task")
        if mandate.is_expired(time.time()):
            violations.append("mandate is expired")
        required = [str(value).strip() for value in (subtask.required_capabilities or [])]
        missing_capabilities = [
            capability for capability in required if capability not in mandate.capabilities
        ]
        if missing_capabilities:
            violations.append(
                "capabilities are not granted: " + ", ".join(missing_capabilities)
            )
        constraints = [str(value).strip() for value in (subtask.file_constraints or [])]
        if constraints:
            if not mandate.resources:
                violations.append("resources allow-list is empty")
            else:
                missing_resources = [
                    path for path in constraints if path not in mandate.resources
                ]
                if missing_resources:
                    violations.append(
                        "resources are not granted: " + ", ".join(missing_resources)
                    )
        attempt_number = int(getattr(subtask, "dispatch_retry_count", 0) or 0) + 1
        if attempt_number > mandate.max_attempts:
            violations.append(
                f"attempt {attempt_number} exceeds max_attempts {mandate.max_attempts}"
            )

        subtask._mandate = mandate  # type: ignore[attr-defined]
        event: dict[str, Any] = {
            "phase": "mandate envelope",
            "percent": 0,
            "mandate_id": mandate.mandate_id,
            "mandate_digest": mandate.digest(),
            "mandate_enforced": enforce,
            "mandate_expired": mandate.is_expired(time.time()),
            "mandate_violations": violations[:8],
        }
        if enforce and violations:
            return event, "Mandate envelope rejected: " + "; ".join(violations[:5])
        return event, None

    def _runtime_diagnosis(self, output: AgentOutput) -> dict[str, Any] | None:
        """Return a SWE-Doctor diagnosis for a failed command output.

        Opt-in via ``UC_SWE_DOCTOR``.  The local harness reports the bounded
        output of the most recent failed command; adapters that cannot observe
        command output leave ``test_output`` empty and this is a no-op.
        """

        from ultimate_coders.acceptance.swe_doctor_diag import (
            diagnose_runtime,
            swe_doctor_enabled,
        )

        if not swe_doctor_enabled() or output.success:
            return None
        text = output.test_output.strip()
        if not text:
            return None
        report = diagnose_runtime(text)
        payload: dict[str, Any] = {
            "phase": "runtime diagnosis",
            "percent": 0,
            "runtime_summary": report.summary,
            "divergence_frame": report.divergence_frame,
            "expected": [item.to_dict() for item in report.expected[:8]],
            "snapshots": [item.to_dict() for item in report.snapshots[:8]],
        }
        if not report.summary and not report.divergence_frame and not report.snapshots:
            return None
        return payload

    async def _verify_output_anti_patterns(
        self,
        working_dir: str | None,
        output: AgentOutput,
    ) -> Any | None:
        """Run the deterministic V10 acceptance anti-pattern gate.

        Opt-in via ``UC_ANTI_PATTERN_GATE``. ``before`` is the worktree's
        ``HEAD`` snapshot of every changed file and ``after`` is the current
        file. The local harness reports exact commands through
        ``AgentOutput.commands_run``; adapters that cannot report commands
        leave it empty, so the gate skips only the unverifiable
        ``authored_test_never_run`` finding rather than inventing evidence.
        """

        from ultimate_coders.acceptance.anti_pattern_gate import (
            AntiPatternReport,
            anti_pattern_gate_enabled,
            scan_anti_patterns,
        )

        if not anti_pattern_gate_enabled() or not working_dir:
            return None
        changes = list(output.file_changes) + list(output.failed_file_changes)
        paths = [fc.file_path for fc in changes if fc.file_path]
        if not paths:
            return None
        root = Path(working_dir)
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for rel in dict.fromkeys(paths):
            path = Path(rel)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError:
                    continue
            key = path.as_posix()
            target = root / path
            try:
                after[key] = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                after[key] = ""
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "show",
                    f"HEAD:{key}",
                    cwd=str(root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
            except OSError:
                continue
            if proc.returncode == 0:
                before[key] = stdout.decode("utf-8", errors="replace")
        if not after:
            return None
        commands = tuple(getattr(output, "commands_run", ()) or ())
        report = scan_anti_patterns(before, after, run_commands=commands)
        if commands:
            return report
        kept = tuple(item for item in report.findings if item.kind != "authored_test_never_run")
        if len(kept) == len(report.findings):
            return report
        errors = tuple(item for item in kept if item.severity == "error")
        summary = "\n".join(
            f"{item.path or '<patch>'}:{item.line}: {item.kind} — {item.detail}"
            for item in errors[:8]
        )
        return AntiPatternReport(
            findings=kept,
            passed=not errors,
            summary=summary or report.summary,
        )

    def _assembled_context_block(
        self, manifest: Any
    ) -> tuple[str, dict[str, Any]] | None:
        """Project a compiled manifest into the five fixed-prefix slots.

        Opt-in via ``UC_PREFIX_CONTEXT_ASSEMBLY``. This does not fetch or
        drop content: every manifest item is placed in exactly one slot, and
        the existing context compiler still owns budgeting and tier priority.
        A slot is marked stable only when every item in it is stable, and the
        stable flag is monotonic across slot order so a volatile search hit
        cannot sit inside a cached prefix.
        """

        from ultimate_coders.agent.context_compiler import Tier
        from ultimate_coders.search.context_assembler import (
            CHECKPOINT,
            DECLARATIVE,
            OBSERVATION,
            PROCEDURAL,
            SYSTEM_SCHEMA,
            ContextAssembler,
            ContextSlot,
            context_assembler_enabled,
        )

        if not context_assembler_enabled():
            return None
        groups: tuple[tuple[str, tuple[Any, ...]], ...] = (
            (SYSTEM_SCHEMA, (Tier.POLICY,)),
            (DECLARATIVE, (Tier.EVIDENCE,)),
            (PROCEDURAL, (Tier.TRACE,)),
            (CHECKPOINT, (Tier.MEMORY,)),
            (OBSERVATION, (Tier.STATE, Tier.BACKGROUND)),
        )
        items = list(getattr(manifest, "items", ()) or ())
        slots: list[Any] = []
        stable_allowed = True
        for slot_name, tiers in groups:
            group = [item for item in items if getattr(item, "tier", None) in tiers]
            if not group:
                continue
            content = "\n\n".join(
                str(getattr(item, "content", "") or "").strip() for item in group
            ).strip()
            if not content:
                continue
            stable = stable_allowed and all(
                bool(getattr(item, "stable", False)) for item in group
            )
            if not stable:
                stable_allowed = False
            slots.append(ContextSlot(slot_name, content, stable=stable))
        if not slots:
            return None
        assembled = ContextAssembler().assemble(
            slots,
            budget_tokens=self._context_budget_tokens(),
        )
        summary = {
            "slots": [block.slot for block in assembled.blocks],
            "full_sha256": assembled.full_sha256,
            "prefix_cache_hint": assembled.prefix_cache_hint,
            "stable_tokens": assembled.stable_tokens,
            "total_tokens": assembled.total_tokens,
            "truncated": assembled.truncated,
            "over_budget": assembled.over_budget,
        }
        return assembled.render(), summary

    async def _changed_patch(
        self,
        working_dir: str,
        output: AgentOutput,
    ) -> str | None:
        """Return a unified diff for every changed file that still exists.

        The before side is read from the worktree's ``HEAD`` so untracked
        files are skipped rather than described as whole-file additions.
        This is the shared patch boundary for the deterministic verifiers.
        """

        changes = list(output.file_changes) + list(output.failed_file_changes)
        paths = [fc.file_path for fc in changes if fc.file_path]
        if not paths:
            return None
        root = Path(working_dir)
        patches: list[str] = []
        import difflib

        for rel in dict.fromkeys(paths):
            path = Path(rel)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError:
                    continue
            key = path.as_posix()
            try:
                after = (root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "show",
                    f"HEAD:{key}",
                    cwd=str(root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
            except OSError:
                continue
            if proc.returncode != 0:
                continue
            before = stdout.decode("utf-8", errors="replace")
            patches.append(
                "".join(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=f"a/{key}",
                        tofile=f"b/{key}",
                        n=0,
                    )
                )
            )
        patch = "".join(patches)
        return patch if patch.strip() else None

    async def _verify_output_retrace(
        self,
        subtask: Subtask,
        working_dir: str | None,
        output: AgentOutput,
    ) -> Any | None:
        """Compare the requirement rationale with a blind patch reconstruction.

        Opt-in via ``UC_RETRACE_VERIFIER``. The verifier is deterministic and
        never sees model output as ground truth: the backward pass only gets
        the patch. The result is diagnostic and never changes the outcome.
        """

        from ultimate_coders.acceptance.retrace_verifier import (
            RetraceVerifier,
            retrace_enabled,
        )

        if not retrace_enabled() or not working_dir or not subtask.description.strip():
            return None
        patch = await self._changed_patch(working_dir, output)
        if patch is None:
            return None
        try:
            verifier = RetraceVerifier()
            rationale = verifier.forward(
                subtask.description,
                {
                    "summary": output.summary,
                    "steps": list(output.tool_calls[-20:]),
                    "notes": list(output.commands_run[-10:]),
                },
            )
            reconstruction = verifier.backward(patch, {})
            return verifier.arbitrate(rationale, reconstruction)
        except Exception:
            logger.debug(
                "retrace verification failed for %s",
                output.summary[:80],
                exc_info=True,
            )
            return None

    def _exec_critic_preflight(
        self,
        subtask: Subtask,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Validate an optional ExecCritic test artifact declared on the task.

        Opt-in via ``UC_EXEC_CRITIC``. A task may carry
        ``agent_config["test_artifact"]`` with the same fields TestGenNode
        consumes (``test_patch``, ``test_command``, ``test_contract``,
        ``base_clean_fail``, ``hidden``, ``frozen``). An invalid artifact is
        diagnostic by default; ``"enforce": true`` makes it fail the subtask
        before any source edit. A valid artifact is retained for the
        source-repair protection check after the run.
        """

        from ultimate_coders.acceptance.exec_critic import (
            STATUS_OK,
            TestGenNode,
            exec_critic_enabled,
        )

        subtask._exec_critic_artifact = None  # type: ignore[attr-defined]
        subtask._exec_critic_enforce = False  # type: ignore[attr-defined]
        if not exec_critic_enabled():
            return None, None
        config = subtask.agent_config if isinstance(subtask.agent_config, dict) else {}
        raw = config.get("test_artifact")
        if not isinstance(raw, dict):
            return None, None
        enforce = bool(raw.get("enforce", False))
        artifact = TestGenNode().generate(subtask.id, {}, raw)
        event = {
            "phase": "exec critic",
            "percent": 0,
            "exec_critic_status": artifact.status,
            "exec_critic_passed": artifact.status == STATUS_OK,
            "exec_critic_enforced": enforce,
            "exec_critic_reason": artifact.reason[:500],
        }
        if artifact.status != STATUS_OK:
            if enforce:
                reason = f"ExecCritic test artifact rejected ({artifact.status}): {artifact.reason}"
                return event, reason[:2000]
            return event, None
        subtask._exec_critic_artifact = artifact  # type: ignore[attr-defined]
        subtask._exec_critic_enforce = enforce  # type: ignore[attr-defined]
        return event, None

    async def _verify_output_exec_critic(
        self,
        subtask: Subtask,
        working_dir: str | None,
        output: AgentOutput,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Keep a declared ExecCritic artifact's tests protected.

        A declared, valid test artifact belongs to the acceptance contract.
        The source repair path may not modify those test files: the changed
        worktree patch is checked through ``SourceRepairNode.repair``.
        Diagnostic unless the task set ``"enforce": true``.
        """

        from ultimate_coders.acceptance.exec_critic import (
            ProtectedTestError,
            SourceRepairNode,
            is_protected_test_path,
        )

        artifact = getattr(subtask, "_exec_critic_artifact", None)
        enforce = bool(getattr(subtask, "_exec_critic_enforce", False))
        if artifact is None or not working_dir:
            return None, None
        changed_paths = [
            fc.file_path
            for fc in list(output.file_changes) + list(output.failed_file_changes)
            if fc.file_path
        ]
        protected = sorted({path for path in changed_paths if is_protected_test_path(path)})
        if protected:
            reason = "source repair may not modify protected test files: " + ", ".join(protected)
            error = f"ExecCritic test protection rejected: {reason}"[:2000] if enforce else None
            return {
                "phase": "exec critic",
                "percent": 0,
                "exec_critic_status": "hard_fail",
                "exec_critic_passed": False,
                "exec_critic_enforced": enforce,
                "exec_critic_reason": reason[:500],
            }, error
        try:
            patch = await self._changed_patch(working_dir, output)
        except Exception:
            logger.debug("exec-critic patch read failed", exc_info=True)
            return None, None
        if patch is None:
            return {
                "phase": "exec critic",
                "percent": 0,
                "exec_critic_status": "ok",
                "exec_critic_passed": True,
                "exec_critic_enforced": enforce,
                "exec_critic_reason": "no changed source patch",
            }, None
        try:
            result = SourceRepairNode().repair(
                subtask.id,
                patch,
                test_patch=artifact.test_patch,
            )
        except ProtectedTestError as exc:
            reason = str(exc)[:500]
            error = f"ExecCritic test protection rejected: {reason}"[:2000] if enforce else None
            return {
                "phase": "exec critic",
                "percent": 0,
                "exec_critic_status": "hard_fail",
                "exec_critic_passed": False,
                "exec_critic_enforced": enforce,
                "exec_critic_reason": reason,
            }, error
        except Exception:
            logger.debug("exec-critic source repair failed", exc_info=True)
            return None, None
        error = None
        if enforce and not result.ok:
            error = f"ExecCritic source repair rejected: {result.reason}"[:2000]
        return {
            "phase": "exec critic",
            "percent": 0,
            "exec_critic_status": result.status,
            "exec_critic_passed": result.ok,
            "exec_critic_enforced": enforce,
            "exec_critic_reason": result.reason[:500],
        }, error

    def _verify_output_boundary(
        self,
        subtask: Subtask,
        output: AgentOutput,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Check changed files against the worker-side BoundaryContract (#7).

        Opt-in via ``UC_BOUNDARY_CONTRACT``. The contract is built from the
        PerceptionPass ``ImplementationSpec`` before execution; any changed
        path that is not ``editable`` is a violation. With
        ``UC_BOUNDARY_CONTRACT_ENFORCE`` the attempt fails; otherwise the
        result rides a ``subtask_progress`` event only. Unlisted paths are
        ``inspect_only`` (fail closed).
        """

        from ultimate_coders.agent.boundary_contract import (
            boundary_contract_enforce_enabled,
        )

        contract = getattr(subtask, "_boundary_contract", None)
        if contract is None:
            return None, None
        changed = [
            change.file_path
            for change in (
                list(output.file_changes) + list(output.failed_file_changes)
            )
            if getattr(change, "file_path", "")
        ]
        if not changed:
            return None, None
        violations = contract.violations(changed)
        enforce = boundary_contract_enforce_enabled()
        event: dict[str, Any] = {
            "phase": "boundary contract",
            "percent": 0,
            "boundary_version": int(contract.version),
            "boundary_editable": list(contract.editable_paths())[:12],
            "boundary_violations": [item.to_dict() for item in violations[:8]],
            "boundary_enforced": enforce,
        }
        if not violations:
            return event, None
        details = "; ".join(
            f"{item.path}:{item.role.value}" for item in violations[:5]
        )
        error = (
            f"Boundary contract rejected: {details}"[:2000] if enforce else None
        )
        return event, error

    async def _verify_output_dockerless(
        self,
        working_dir: str | None,
        output: AgentOutput,
    ) -> Any | None:
        """Run the deterministic V10 dockerless patch verifier for observability.

        Opt-in via ``UC_DOCKERLESS_VERIFIER``. The verifier never executes
        code: it scores signature compatibility, lock-release paths and
        boundary conditions from the changed-file diff. The worker currently
        supplies an empty ``repo_index`` and no call chain, so this is a
        patch-local signal, not an Oracle replacement; the report rides a
        ``subtask_progress`` event and never changes the subtask outcome.
        """

        from ultimate_coders.acceptance.dockerless_verifier import (
            DockerlessAgenticVerifier,
            dockerless_verifier_enabled,
        )

        if not dockerless_verifier_enabled() or not working_dir:
            return None
        patch = await self._changed_patch(working_dir, output)
        if patch is None:
            return None
        try:
            return DockerlessAgenticVerifier().verify(patch, repo_index={})
        except Exception:
            logger.debug(
                "dockerless verification failed for %s",
                output.summary[:80],
                exc_info=True,
            )
            return None

    async def _verify_output_process(
        self,
        working_dir: str | None,
        output: AgentOutput,
    ) -> Any | None:
        """Run the deterministic process review over changed test files.

        Opt-in via ``UC_PROCESS_VERIFIER_ENFORCE``.  The check compares the
        test file at the worktree's HEAD with its post-run content, so it
        catches deleted assertions and test bypasses before the subtask is
        reported as successful.  The report is returned for observability;
        callers decide whether to fail the result.
        """

        from ultimate_coders.architecture.process_verifier import (
            process_verifier_enabled,
            verify_process,
        )

        if not process_verifier_enabled() or not working_dir:
            return None
        changed = [
            fc.file_path
            for fc in output.file_changes
            if fc.file_path and _is_test_file(Path(fc.file_path))
        ]
        if not changed:
            return None
        root = Path(working_dir)
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for rel in dict.fromkeys(changed):
            path = Path(rel)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError:
                    continue
            target = root / path
            try:
                after[path.as_posix()] = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                after[path.as_posix()] = ""
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "show",
                    f"HEAD:{path.as_posix()}",
                    cwd=str(root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
            except OSError:
                continue
            if proc.returncode == 0:
                before[path.as_posix()] = stdout.decode("utf-8", errors="replace")
        if not after:
            return None
        return verify_process(before, after, test_paths=tuple(after))

    async def _execute_steps(
        self,
        subtask: Subtask,
        working_dir: str | None,
        on_stdout_line: Any,
        context_block: str,
        cancel_key: tuple[str, str] | None = None,
    ) -> AgentOutput:
        """Run a subtask's ordered multi-agent workflow steps.

        Each step runs its configured agent (grok-build / claude-code / codex) with a
        prompt template. Templates support:
          {{prev_summary}} — previous step's AgentOutput.summary
          {{prev_files}}   — previous step's modified file paths (one/line)
          {{step<N>.summary}} / {{step<N>.files}} — any prior step by index

        Step 0 has no prev; {{prev_*}} resolves to empty for it. A failed
        step aborts the chain (unless abort_on_failure=False) and the
        subtask fails. The returned AgentOutput is the LAST step's output;
        file_changes are accumulated across all steps so the SubtaskResult
        reflects every file touched in the workflow.

        Steps with a non-empty ``parallel_group`` run concurrently via
        ``asyncio.gather`` (consecutive same-group steps form one group).
        Parallel-group steps MUST be read-only (disallowed_tools includes
        Edit, Write, Bash) or the subtask fails — this prevents worktree
        corruption from concurrent writes.
        """
        step_outputs: list[AgentOutput] = []
        all_file_changes: list[FileChange] = []
        domain_steps: list[dict[str, Any]] = []
        failed_file_changes: list[FileChange] = []
        had_failed_step = False
        # T18 #668: one record per *executed* unit, in execution order. A step
        # skipped by its condition contributes nothing here (see
        # `_step_usage`), which is what makes a gap in `step_index` readable as
        # "that step did not run".
        step_usages: list[StepUsage] = []
        last_output: AgentOutput | None = None
        file_constraints_str = ", ".join(subtask.file_constraints) or "none"

        # Group consecutive steps by parallel_group. A step with empty
        # parallel_group runs sequentially (current behavior). A run of
        # steps with the SAME non-empty parallel_group runs concurrently
        # via asyncio.gather. Non-consecutive same-group steps are
        # separate groups (simpler; documented).
        idx = 0
        total = len(subtask.steps)
        while idx < total:
            step = subtask.steps[idx]
            group = step.parallel_group

            if not group:
                # Sequential step — run directly via the shared helper.
                prev = step_outputs[-1] if step_outputs else None
                output = await self._run_single_step(
                    step,
                    idx,
                    total,
                    step_outputs,
                    prev,
                    context_block,
                    file_constraints_str,
                    subtask,
                    working_dir,
                    on_stdout_line,
                    cancel_key=cancel_key,
                    parallel_group=step.parallel_group or "",
                    parallel_step_count=1,
                )
                if output is None:
                    # Step was skipped (condition false) — no output appended.
                    idx += 1
                    continue
                step_outputs.append(output)
                # T18 #668: collected here — *before* the abort check below —
                # so a step that failed and aborted the chain is still
                # attributed. It ran, so it spent.
                step_usages.append(_step_usage(idx, step.parallel_group or "", step.agent, output))
                if output.success:
                    all_file_changes.extend(output.file_changes)
                    if output.domain_result is not None:
                        domain_steps.append(
                            {"index": idx, "agent": step.agent, "result": output.domain_result}
                        )
                else:
                    had_failed_step = True
                    failed_file_changes.extend(output.file_changes)
                last_output = output

                if not output.success and (
                    step.abort_on_failure or not output.retryable or output.cleanup_pending
                ):
                    logger.warning(
                        "Workflow step %d failed (agent=%s), aborting chain for subtask %s",
                        idx + 1,
                        step.agent,
                        subtask.id[:8],
                    )
                    return AgentOutput(
                        summary=f"[step {idx + 1} ({step.agent}) failed] {output.summary}",
                        file_changes=all_file_changes,
                        token_usage=output.token_usage,
                        success=False,
                        retryable=output.retryable,
                        cleanup_pending=output.cleanup_pending,
                        stderr_tail=output.stderr_tail,
                        tool_calls=output.tool_calls,
                        # T18 #668: the chain stopped early, but the units that
                        # DID run are still what this node spent — dropping them
                        # here would recreate the under-report on the failure
                        # path. Includes the failing step (collected above the
                        # abort check).
                        step_usages=step_usages,
                        had_failed_step=had_failed_step,
                        failed_file_changes=failed_file_changes,
                    )
                # abort_on_failure=False: log and continue to next step.
                if not output.success:
                    logger.warning(
                        "Workflow step %d failed but abort_on_failure=False; continuing chain",
                        idx + 1,
                    )
                idx += 1
                continue

            # Parallel group — collect all consecutive steps with the same group.
            group_steps: list[tuple[int, WorkflowStep]] = []
            while idx < total and subtask.steps[idx].parallel_group == group:
                group_steps.append((idx, subtask.steps[idx]))
                idx += 1

            # VALIDATE: every step in a parallel group MUST be read-only.
            # disallowed_tools must include Edit, Write, and Bash. This is
            # the worktree-conflict mitigation — read-only steps don't
            # mutate the shared worktree. A write-capable step in a group
            # is a hard error (subtask fails).
            required_disallowed = {"Edit", "Write", "Bash"}
            for step_idx, gs in group_steps:
                base_cfg = self._resolve_agent_config(subtask) or {}
                merged_cfg = {**base_cfg, **gs.agent_config}
                from ultimate_coders.inference.agent import InferenceInfraAgent

                if InferenceInfraAgent.requires_workspace(merged_cfg):
                    return AgentOutput(
                        success=False,
                        retryable=False,
                        summary="Mutating inference steps cannot share a parallel workspace",
                    )
                disallowed = set(merged_cfg.get("disallowed_tools", []) or [])
                missing = required_disallowed - disallowed
                if missing:
                    missing_str = ", ".join(sorted(missing))
                    logger.error(
                        "Workflow step %d parallel_group='%s' must be read-only "
                        "(disallowed_tools must include Edit, Write, Bash); missing: %s",
                        step_idx + 1,
                        group,
                        missing_str,
                    )
                    return AgentOutput(
                        summary=(
                            f"[step {step_idx + 1} parallel_group='{group}' "
                            f"must be read-only (disallowed_tools must include "
                            f"Edit, Write, Bash)]"
                        ),
                        file_changes=all_file_changes,
                        success=False,
                    )

            # All steps in the group share the SAME prev (last output before
            # the group). They do NOT see each other's outputs mid-group.
            prev = step_outputs[-1] if step_outputs else None

            # Run all steps in the group concurrently. Each step's
            # condition/retry/event logic runs inside _run_single_step.
            # We pass a snapshot of step_outputs (copy) so concurrent
            # steps don't see each other's mid-flight mutations.
            prev_snapshot = list(step_outputs)
            group_size = len(group_steps)
            results = await asyncio.gather(
                *[
                    self._run_single_step(
                        gs,
                        si,
                        total,
                        prev_snapshot,
                        prev,
                        context_block,
                        file_constraints_str,
                        subtask,
                        working_dir,
                        on_stdout_line,
                        cancel_key=cancel_key,
                        parallel_group=group,
                        parallel_step_count=group_size,
                    )
                    for si, gs in group_steps
                ]
            )

            # Collect outputs in order. Skipped steps (condition false)
            # return None — they don't contribute to step_outputs. A
            # condition parse error returns success=False (subtask fails).
            group_failed = False
            failed_output: AgentOutput | None = None
            for i, (si, gs) in enumerate(group_steps):
                output = results[i]
                if output is None:
                    continue
                step_outputs.append(output)
                # T18 #668: one record per group MEMBER — a parallel group of N
                # produces N entries and deliberately no group-level total,
                # because a sum would make the members mutually unattributable
                # (same reason the chain's own usage is never collapsed).
                # Collected before the abort check so a failing member is
                # attributed too.
                step_usages.append(_step_usage(si, group, gs.agent, output))
                if output.success:
                    all_file_changes.extend(output.file_changes)
                    if output.domain_result is not None:
                        domain_steps.append(
                            {"index": si, "agent": gs.agent, "result": output.domain_result}
                        )
                else:
                    had_failed_step = True
                    failed_file_changes.extend(output.file_changes)
                last_output = output
                if not output.success and (
                    gs.abort_on_failure or not output.retryable or output.cleanup_pending
                ):
                    group_failed = True
                    failed_output = output
                    logger.warning(
                        "Workflow step %d failed (agent=%s, parallel_group=%s), "
                        "aborting chain for subtask %s",
                        si + 1,
                        gs.agent,
                        group,
                        subtask.id[:8],
                    )
                    break

            if group_failed and failed_output is not None:
                return AgentOutput(
                    summary=f"[step failed in parallel_group='{group}'] {failed_output.summary}",
                    file_changes=all_file_changes,
                    token_usage=failed_output.token_usage,
                    success=False,
                    retryable=failed_output.retryable,
                    cleanup_pending=failed_output.cleanup_pending,
                    stderr_tail=failed_output.stderr_tail,
                    tool_calls=failed_output.tool_calls,
                    # T18 #668: same as the sequential abort — the members that
                    # ran before the failing one keep their records. Members of
                    # this group that the `break` skipped over were never
                    # collected, exactly as they were never appended to
                    # `step_outputs` (pre-existing behaviour, not new here).
                    step_usages=step_usages,
                    had_failed_step=had_failed_step,
                    failed_file_changes=failed_file_changes,
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
                # T18 #668: deliberately left empty rather than filled with a
                # synthetic entry — no unit ran, so there is nothing to
                # attribute (the same rule that keeps `usage` at `None` when
                # nothing was reported).
                step_usages=[],
            )
        # T15 #660 note: every return in this chain forwards ONE step's usage
        # (`last_output` here, the failing step on the two abort paths above).
        # It is deliberately NOT summed: a workflow can run several adapters, so
        # a single collapsed block would make both the number and its `source`
        # unattributable.
        #
        # T18 #668 superseded the tail of that note. It used to read: "Per-step
        # usage belongs on per-step events; until that exists this is an
        # under-report of a multi-step chain, not a total." The under-report was
        # real, and `step_usages` below is where the per-step detail now lives —
        # on the terminal event's `payload.steps[]`, not on separate events (#666
        # ruled out the N-events / new-event-type shapes precisely because both
        # would have to re-answer whether a fenced late result refuses them too).
        # The node-level fields still carry one step's numbers: that is
        # deliberate and unchanged, because making them the sum would break
        # comparability with historical rows and re-open the "exactly once"
        # argument `commit_once` settles structurally. `payload.steps[]` is what
        # makes the shortfall readable instead.
        return AgentOutput(
            summary=last_output.summary,
            domain_result={"steps": domain_steps} if domain_steps else None,
            file_changes=all_file_changes,
            token_usage=last_output.token_usage,
            # Ordinary chains keep last-step success, including a non-aborting
            # earlier failure. Explicit review fail-closed uses had_failed_step
            # and failed_file_changes in _execute_in_sandbox.
            success=last_output.success,
            retryable=last_output.retryable,
            cleanup_pending=last_output.cleanup_pending,
            stderr_tail=last_output.stderr_tail,
            tool_calls=last_output.tool_calls,
            step_usages=step_usages,
            had_failed_step=had_failed_step,
            failed_file_changes=failed_file_changes,
        )

    async def _run_single_step(
        self,
        step: WorkflowStep,
        idx: int,
        total: int,
        step_outputs: list[AgentOutput],
        prev: AgentOutput | None,
        context_block: str,
        file_constraints_str: str,
        subtask: Subtask,
        working_dir: str | None,
        on_stdout_line: Any,
        cancel_key: tuple[str, str] | None = None,
        parallel_group: str = "",
        parallel_step_count: int = 1,
    ) -> AgentOutput | None:
        """Run a single workflow step: render → condition → retry → execute → emit.

        Shared by both the sequential path and the parallel-group path in
        ``_execute_steps``. Returns the step's ``AgentOutput``, or ``None``
        if the step was skipped (condition evaluated to false).

        This helper does NOT append to ``step_outputs`` — the caller is
        responsible for appending the result (so parallel groups can
        collect outputs after gather completes, avoiding mid-flight list
        mutation).

        ``parallel_group`` / ``parallel_step_count`` are emitted in every
        step event so consumers can distinguish concurrent from sequential
        steps and show a "N parallel" indicator. Empty group + count 1 =
        sequential (backward compatible).
        """
        rendered = self._render_step_prompt(
            step.prompt,
            idx,
            step_outputs,
            prev,
            context_block,
            file_constraints_str,
        )
        # Step templates opt into dependency context with {{context}}. An
        # explicit review still has to see the verdict contract when the
        # template omits that placeholder. Single-agent execution prepends
        # the same block unconditionally.
        if "review" in subtask.required_capabilities and REVIEW_INSTRUCTIONS not in rendered:
            prefix = context_block.strip()
            if prefix:
                rendered = f"{prefix}\n\n{rendered}"

        # ponytail: per-step agent_config override — merge step config over
        # the subtask-level resolved config (step wins on conflict).
        base_cfg = self._resolve_agent_config(subtask) or {}
        merged_cfg = {**base_cfg, **step.agent_config} or None
        if step.agent in ("local-harness", "local-llm"):
            merged_cfg = {
                **(merged_cfg or {}),
                **self._drift_hints(subtask),
                **self._radio_hints(subtask),
            }
        if step.agent in ("metainfer", "inference-infra"):
            merged_cfg = {
                **(merged_cfg or {}),
                "uc_execution": {
                    "graph_id": subtask.parent_id,
                    "node_id": subtask.id,
                    "attempt_id": subtask.dispatch_retry_count,
                    "step_index": idx,
                },
            }
            state = await self._domain_state()
            merged_cfg["_uc_runtime_state_dir"] = str(state.path.parent)
            if self._workspace_manager:
                merged_cfg["_uc_workspace_owner"] = await self._workspace_manager.runner_owner(
                    working_dir
                )

        logger.info(
            "Workflow step %d/%d (subtask %s): agent=%s",
            idx + 1,
            total,
            subtask.id[:8],
            step.agent,
        )
        percent = int(100 * idx / total) if total else 0

        # Condition evaluation: skip this step if the expression is false.
        # Empty condition = always run (backward compat). Parse error →
        # subtask fails with a clear message (never silently run/skip).
        if step.condition:
            try:
                should_run = evaluate(step.condition, prev)
            except ConditionError as e:
                logger.error(
                    "Workflow step %d condition parse error: %s",
                    idx + 1,
                    e,
                )
                return AgentOutput(
                    summary=f"[step {idx + 1} condition parse error] {e}",
                    file_changes=[],
                    success=False,
                )
            if not should_run:
                logger.info(
                    "Workflow step %d/%d skipped (condition false): %s",
                    idx + 1,
                    total,
                    step.condition,
                )
                await self._emit_step_event(
                    subtask,
                    "subtask_progress",
                    phase=f"step {idx + 1}/{total}: {step.agent} (skipped)",
                    percent=percent,
                    step_index=idx,
                    step_total=total,
                    step_agent=step.agent,
                    step_status="skipped",
                    step_summary=step.condition[:200],
                    parallel_group=parallel_group,
                    parallel_step_count=parallel_step_count,
                )
                return None

        await self._emit_step_event(
            subtask,
            "subtask_progress",
            phase=f"step {idx + 1}/{total}: {step.agent}",
            percent=percent,
            step_index=idx,
            step_total=total,
            step_agent=step.agent,
            step_status="started",
            parallel_group=parallel_group,
            parallel_step_count=parallel_step_count,
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
                cancel_key=cancel_key,
            )
            if (
                output.success
                or not output.retryable
                or output.cleanup_pending
                or attempt == max_attempts - 1
            ):
                break
            # Failed and retries remain — emit retrying event + sleep.
            await self._emit_step_event(
                subtask,
                "subtask_progress",
                phase=f"step {idx + 1}/{total}: {step.agent}",
                percent=percent,
                step_index=idx,
                step_total=total,
                step_agent=step.agent,
                step_status="retrying",
                retry_attempt=attempt + 1,
                step_summary=output.summary[:200],
                parallel_group=parallel_group,
                parallel_step_count=parallel_step_count,
            )
            if step.retry_delay_ms > 0:
                await asyncio.sleep(step.retry_delay_ms / 1000)
        assert output is not None  # loop runs at least once

        # Only accumulate file changes from successful steps — a failed
        # step's partial edits are not a reliable result. ponytail: avoid
        # reporting partial work as applied when a later step succeeds.
        # NOTE: file_changes accumulation is done by the CALLER, not here.
        await self._emit_step_event(
            subtask,
            "subtask_progress",
            phase=f"step {idx + 1}/{total}: {step.agent}",
            percent=int(100 * (idx + 1) / total) if total else 0,
            step_index=idx,
            step_total=total,
            step_agent=step.agent,
            step_status="completed" if output.success else "failed",
            step_summary=output.summary[:200],
            parallel_group=parallel_group,
            parallel_step_count=parallel_step_count,
        )
        return output

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

        # ponytail: F64 — NATS default max_payload is 1MB; a sandbox may edit
        # 500MB files. Reading/embedding those spikes memory and the publish
        # raises (silently dropped before) — the gateway never re-indexes the
        # file. Cap at 512KB and send a marker the gateway can distinguish
        # from empty/deleted content.
        max_content_bytes = 512 * 1024
        for fc in result.modified_files:
            content = ""
            if base_path and fc.change_type.value != "deleted":
                full = os.path.join(base_path, fc.file_path)
                try:
                    if os.path.getsize(full) > max_content_bytes:
                        content = "[content omitted: exceeds 512KB — re-index from source]"
                        logger.warning(
                            "File %s too large to broadcast; sending marker",
                            fc.file_path,
                        )
                    else:
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
                # ponytail: F64 — was debug-only: a dropped broadcast meant the
                # gateway silently never re-indexed this file.
                logger.warning(
                    "Failed to broadcast file change for %s",
                    fc.file_path,
                    exc_info=True,
                )

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

    def _progress_stall_limit(self) -> int:
        """Consecutive no-evidence attempts before the retry loop stops.

        ``UC_PROGRESS_STALL_LIMIT`` overrides the default (2). Values below
        1 are clamped: with a limit of 0 every repeated failure would stop
        immediately, including one that a single retry would have fixed.
        """
        try:
            value = int(os.environ.get("UC_PROGRESS_STALL_LIMIT", "2"))
        except ValueError:
            value = 2
        return max(1, value)

    def _context_budget_tokens(self) -> int:
        """Token budget shared by the local context sources.

        ``UC_CONTEXT_BUDGET_TOKENS`` overrides the default. 2000 tokens is
        roughly twice the old dependency-only cap (4000 characters ~= 1000
        tokens) and gives the previously unbounded search block a ceiling.
        Values below 256 are clamped: a smaller prompt cannot carry the
        dependency results the graph plane exists to deliver.
        """
        try:
            value = int(os.environ.get("UC_CONTEXT_BUDGET_TOKENS", "2000"))
        except ValueError:
            value = 2000
        return max(256, value)

    async def _build_search_context(self, subtask: Subtask) -> str | None:
        """Search across repos for code relevant to the subtask description.

        Returns a formatted context block with search results, or None.
        ponytail: F59 — async: search + repo discovery are gRPC RPCs that
        blocked the event loop (runs at every subtask start).
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
                # ponytail: F59 — inline in_all_repos with the async repo
                # discovery (SearchQuery.in_all_repos calls sync list_repos,
                # another blocking RPC). Mirrors its repo_id extraction.
                repos = await _engine_call(
                    self.engine,
                    "list_repos",
                    "list_repos_async",
                )
                sq.in_repos([r.repo_id if hasattr(r, "repo_id") else str(r) for r in (repos or [])])
            d = sq.to_dict()
            cache_key = WorkerLocalCache.search_key(
                d["query"],
                d["repo_ids"],
                d["modes"],
                d["max_results"],
            )
            result = self._search_cache.get_search(cache_key)
            if result is None:
                result = await _engine_call(
                    self.engine,
                    "search",
                    "search_async",
                    sq,
                )
                if result is not None:
                    self._search_cache.put_search(cache_key, result)
            items = getattr(result, "items", result) if result else []
            if not items:
                return None
            # MemCredit arbitration is opt-in: it turns the raw hit list into a
            # budget-bounded, de-duplicated selection measured in tokens rather
            # than a fixed "first eight snippets" cut.  The flag is default-off
            # so the legacy prompt stays byte-identical until an operator asks
            # for the upgraded retrieval face.
            from ultimate_coders.architecture.memcredit import (
                MemCreditCandidate,
                memcredit_enabled,
                select_memcredit,
            )

            if memcredit_enabled():
                candidates = []
                for index, item in enumerate(items):
                    repo = getattr(item, "repo_id", "?")
                    path = getattr(item, "file_path", "?")
                    snippet = getattr(item, "content_snippet", "") or ""
                    match_type = str(getattr(item, "match_type", "text") or "text")
                    if match_type not in {"text", "ast", "qdrant"}:
                        match_type = "text"
                    candidates.append(
                        MemCreditCandidate(
                            identifier=(
                                f"{repo}:{path}:"
                                f"{getattr(item, 'start_line', 0)}-"
                                f"{getattr(item, 'end_line', 0)}:{index}"
                            ),
                            content=snippet,
                            path=str(path),
                            score=float(getattr(item, "score", 0.0) or 0.0),
                            stage=match_type,
                            symbol=str(getattr(item, "symbol_name", "") or ""),
                            metadata={"repo": str(repo), "path": str(path)},
                        )
                    )
                selection = select_memcredit(
                    candidates,
                    attempt=int(getattr(subtask, "dispatch_retry_count", 0) or 0),
                )
                chosen = selection.selected
            else:
                chosen = items[:8]
            if not chosen:
                return None
            lines = ["## Related code from indexed repositories"]
            for r in chosen:
                if isinstance(r, MemCreditCandidate):
                    repo = str(r.metadata.get("repo", "?"))
                    path = str(r.metadata.get("path", r.path or "?"))
                    snippet = r.content
                else:
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

    async def search_across_repos(
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

        # ponytail: F59 — async repo discovery + search (see _build_search_context).
        repos = await _engine_call(self.engine, "list_repos", "list_repos_async")
        sq = SearchQuery(query).in_repos(
            [r.repo_id if hasattr(r, "repo_id") else str(r) for r in (repos or [])]
        )
        if modes:
            sq.with_modes(modes)
        sq.limit(max_results)
        result = await _engine_call(self.engine, "search", "search_async", sq)
        return getattr(result, "items", result) if result else None

    async def read_shared_memory(
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
        # ponytail: F59 — async engine call (sync version blocked the loop in
        # gRPC mode).
        return await _engine_call(
            self.engine,
            "read_memory",
            "read_memory_async",
            key_scope=scope,
            key=key,
            project_id=pid or None,
        )

    async def write_shared_memory(
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
            # ponytail: F59 — async engine call (sync version blocked the loop
            # in gRPC mode).
            result = await _engine_call(
                self.engine,
                "write_memory",
                "write_memory_async",
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

    async def delete_shared_memory(self, key: str, project_id: str = "") -> bool:
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
            # ponytail: F59 — async engine call (sync version blocked the loop
            # in gRPC mode).
            await _engine_call(
                self.engine,
                "delete_memory",
                "delete_memory_async",
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
