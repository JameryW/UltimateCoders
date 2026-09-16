"""Agent data types — Task, Subtask, WorkerInfo, and related enums."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    """Status of a top-level task."""
    CREATED = "created"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class SubtaskStatus(Enum):
    """Status of a subtask."""
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CONFLICTED = "conflicted"


class ChangeType(Enum):
    """Type of file change."""
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class DispatchMode(Enum):
    """How a subtask should be dispatched to workers."""
    REMOTE = "remote"          # Must execute on remote worker, fail after 3 retries
    PREFER_REMOTE = "prefer_remote"  # Prefer remote, fallback to Pending (default)
    # T5 #641 / D4 #633 Q1: DispatchMode.Local was removed (Rust deleted the
    # variant first; legacy "local" wire values parse back to PREFER_REMOTE).


@dataclass
class FileChange:
    """A file change produced by a worker."""
    file_path: str = ""
    change_type: ChangeType = ChangeType.MODIFIED
    diff: str = ""


class AdaptationStrategy(Enum):
    """How a Worker adapted after a failure."""
    NONE = "none"  # no adaptation needed
    SHRINK_SCOPE = "shrink_scope"  # timeout → reduce scope/timeout
    FALLBACK_TOOL = "fallback_tool"  # tool_not_found → use alternative tool
    PURE_LLM = "pure_llm"  # engine_error → skip tools, LLM-only
    WAIT_RETRY = "wait_retry"  # conflict_detected → wait then retry


@dataclass
class SubtaskUsage:
    """Token / cost usage reported by an executor (T15 #660, D13 #657).

    Mirror of the Rust ``uc_types::SubtaskUsage``. The field names are the
    cross-language wire contract: ``_make_task_update_payload`` emits this dict
    onto ``uc.task.update`` and the Rust gateway deserializes it with
    ``serde_json``, which ignores unknown keys — so renaming a field on one
    side only is a *silent* data loss, not an error. ``test_types.py`` pins the
    names for that reason.

    Every field is optional on purpose: adapters report different subsets (a
    CLI adapter may report tokens but no price; others report neither), and a
    missing field means "unknown", **not** zero. ``execution_events.cost`` /
    ``tokens`` must stay NULL for an unreported measure — writing 0 would be
    indistinguishable from a genuine zero (D13's hard requirement).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    # Which adapter produced this usage. Provenance, not a measurement.
    source: str | None = None

    def is_empty(self) -> bool:
        """Whether the block carries no usable number at all.

        ``source`` is deliberately excluded: a block that names the adapter
        but reports no number is *not* a measurement, and letting ``source``
        make it look non-empty would have the gateway claim
        ``usage_reported`` over two NULL columns.
        """
        return (
            self.input_tokens is None
            and self.output_tokens is None
            and self.total_cost_usd is None
        )

    def to_dict(self) -> dict[str, Any]:
        """Wire/checkpoint form — absent keys are omitted, never ``null``.

        Emitting explicit nulls would be equivalent for the Rust side (both
        parse to ``None``) but would defeat the "publisher stays byte-identical
        to pre-T15" property this additive change relies on.
        """
        out: dict[str, Any] = {}
        for key in ("input_tokens", "output_tokens", "total_cost_usd", "source"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubtaskUsage:
        """Parse the wire/checkpoint form.

        Tolerant by design: one malformed optional field degrades to ``None``
        ("unknown") rather than aborting the whole task snapshot.
        """
        def _int(key: str) -> int | None:
            value = data.get(key)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def _float(key: str) -> float | None:
            value = data.get(key)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        source = data.get("source")
        return cls(
            input_tokens=_int("input_tokens"),
            output_tokens=_int("output_tokens"),
            total_cost_usd=_float("total_cost_usd"),
            source=str(source) if source else None,
        )


@dataclass
class SubtaskReview:
    """A review verdict attached to a subtask result (T16 #661, D14 #658).

    Field names mirror the TS ``SubtaskDef.review`` **verbatim**: T6 #642
    deleted the TS review pipeline but deliberately kept that field so a future
    producer could repopulate it, and the TUI already renders it. Renaming
    anything here blanks the verdict in the UI.

    ``None`` on ``SubtaskResult.review`` means "not reviewed", which is **not**
    the same as ``approved=False`` — an unreviewed subtask must render no
    verdict line at all.
    """

    approved: bool = False
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "issues": list(self.issues),
            "suggestions": list(self.suggestions),
        }

    @classmethod
    def from_dict(cls, data: Any) -> SubtaskReview | None:
        """Tolerant of garbage: a malformed block is "no verdict".

        Returns ``None`` (not ``approved=False``) when the block is unusable —
        inventing a rejection out of a parse failure would put a ✗ on a
        subtask nobody ever reviewed.
        """
        if not isinstance(data, dict):
            return None
        approved = data.get("approved")
        if not isinstance(approved, bool):
            return None
        issues = data.get("issues")
        suggestions = data.get("suggestions")
        return cls(
            approved=approved,
            issues=[str(i) for i in issues] if isinstance(issues, list) else [],
            suggestions=(
                [str(s) for s in suggestions] if isinstance(suggestions, list) else []
            ),
        )


@dataclass
class SubtaskResult:
    """Result from a completed subtask."""
    subtask_id: str = ""
    worker_id: str = ""
    modified_files: list[FileChange] = field(default_factory=list)
    summary: str = ""
    success: bool = True
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    adaptation_strategy: AdaptationStrategy = AdaptationStrategy.NONE
    # Failure context (populated on failure)
    stderr_tail: str = ""  # last ~10 lines of stderr
    recent_tool_calls: list[str] = field(default_factory=list)  # last ~5 tool names
    retry_count: int = 0  # how many retries this subtask used
    error: str = ""  # error message on failure
    # Token/cost usage reported by the executor (T15 #660).
    #
    # ``None`` means "not reported" and must stay that way: constructing a
    # zeroed ``SubtaskUsage`` at an error/timeout site would fabricate a
    # measurement (``execution_events.tokens = 0``), which is exactly what
    # D13 forbids. Only a path that actually ran an agent and got usage from it
    # may set this.
    usage: SubtaskUsage | None = None
    # Review verdict from a review node (T16 #661).
    #
    # ``None`` means "not reviewed" — never fabricate a verdict, for the same
    # reason D13 forbids a zeroed usage block: the UI would render a ✗ that
    # nobody issued.
    review: SubtaskReview | None = None


@dataclass
class Subtask:
    """A subtask assigned to a worker."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_id: str = ""
    description: str = ""
    status: SubtaskStatus = SubtaskStatus.PENDING
    assigned_worker: str | None = None
    depends_on: list[str] = field(default_factory=list)
    priority: int = 0
    file_constraints: list[str] = field(default_factory=list)
    expected_output: str = ""
    result: SubtaskResult | None = None
    retry_count: int = 0
    timeout_seconds: int = 0  # 0 = use default
    dispatch_mode: DispatchMode = DispatchMode.PREFER_REMOTE
    dispatch_retry_count: int = 0
    required_capabilities: list[str] = field(default_factory=list)
    # Per-subtask agent config overrides (keys: tools, allowed_tools,
    # disallowed_tools, mcp_configs, append_system_prompt, agent_name, agents_json)
    agent_config: dict[str, Any] = field(default_factory=dict)
    # Ordered multi-agent workflow steps. Empty = single-agent execution via
    # agent_config (backward compatible). When non-empty, the worker runs steps
    # in order, threading each step's AgentOutput into the next step's prompt.
    steps: list[WorkflowStep] = field(default_factory=list)
    # Project scope for cross-repo search and memory sharing
    project_id: str = ""

    @property
    def is_ready(self) -> bool:
        """Whether this subtask has no unmet dependencies."""
        return self.status == SubtaskStatus.PENDING

    @property
    def is_complete(self) -> bool:
        """Whether this subtask has completed successfully."""
        return self.status == SubtaskStatus.COMPLETED

    @property
    def is_failed(self) -> bool:
        """Whether this subtask has failed."""
        return self.status == SubtaskStatus.FAILED


def _resolve_agent_config_field(data: dict[str, Any]) -> dict[str, Any]:
    """Extract an agent_config dict from a payload dict.

    Handles two sources of mismatch in the wire format:
    1. Key name: Rust (NatsSubtaskExecute, WorkflowStepProto) serializes the
       field as ``agent_config_json``; the OMP/Python path uses
       ``agent_config``. Accept either.
    2. Value type: Rust sends a JSON *string*; Python/OMP send a dict.
       Parse the string when present.

    ponytail: best-effort — returns {} on any parse failure so a malformed
    override never crashes the worker; the step still runs with defaults.
    """
    raw = data.get("agent_config")
    if raw is None:
        raw = data.get("agent_config_json")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


@dataclass
class WorkflowStep:
    """A single step in a subtask's multi-agent workflow.

    Each step runs one coding agent (grok-build / claude-code / codex) with a prompt
    template. Steps run sequentially; the previous step's AgentOutput is
    available to the next step's prompt via template variables:
      {{prev_summary}} — previous step's AgentOutput.summary
      {{prev_files}}   — previous step's modified file paths (one per line)
      {{step0.summary}}, {{step0.files}} — any prior step by index
    """

    agent: str = ""
    prompt: str = ""
    # Per-step agent config overrides (same shape as Subtask.agent_config).
    agent_config: dict[str, Any] = field(default_factory=dict)
    # If True (default), a failed step aborts the whole chain and the
    # subtask fails. If False, the chain continues to the next step.
    abort_on_failure: bool = True
    # Number of times to retry this step on failure (0 = no retry, default).
    retry_count: int = 0
    # Delay in ms between retry attempts (0 = retry immediately).
    retry_delay_ms: int = 0
    # Optional condition expression. Evaluated against prior step outputs
    # before running this step; step is skipped if false. Empty = always run.
    condition: str = ""
    # Optional parallel group. Steps sharing a non-empty group run concurrently
    # via asyncio.gather. Steps in a parallel_group MUST be read-only
    # (disallowed_tools includes Edit, Write, Bash) or the subtask fails.
    # Empty = sequential (current behavior, backward compatible).
    parallel_group: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "prompt": self.prompt,
            "agent_config": self.agent_config,
            "abort_on_failure": self.abort_on_failure,
            "retry_count": self.retry_count,
            "retry_delay_ms": self.retry_delay_ms,
            "condition": self.condition,
            "parallel_group": self.parallel_group,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowStep:
        return cls(
            agent=data.get("agent", ""),
            prompt=data.get("prompt", ""),
            agent_config=_resolve_agent_config_field(data),
            abort_on_failure=data.get("abort_on_failure", True),
            retry_count=int(data.get("retry_count", 0) or 0),
            retry_delay_ms=int(data.get("retry_delay_ms", 0) or 0),
            condition=data.get("condition", "") or "",
            parallel_group=data.get("parallel_group", "") or "",
        )


@dataclass
class Task:
    """A top-level task submitted by the user."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    project_id: str = ""
    status: TaskStatus = TaskStatus.CREATED
    subtasks: list[Subtask] = field(default_factory=list)
    result: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Optional verification command (e.g. "cargo check") threaded from the
    # scheduler → aggregator. When set, _aggregate_results passes it to
    # aggregate(verify_command=) → AggregatedResult.verification_passed.
    verify_command: str | None = None

    def update_timestamp(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(timezone.utc)

    # ponytail: to_dict for checkpoint serialization — called by orchestrator.checkpoint_task
    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization (checkpoint/recovery)."""
        return {
            "__version": 1,
            "id": self.id,
            "description": self.description,
            "project_id": self.project_id,
            "status": self.status.value,
            "subtasks": [
                {
                    "id": st.id,
                    "parent_id": st.parent_id,
                    "description": st.description,
                    "status": st.status.value,
                    "assigned_worker": st.assigned_worker,
                    "depends_on": st.depends_on,
                    "priority": st.priority,
                    "file_constraints": st.file_constraints,
                    "expected_output": st.expected_output,
                    "retry_count": st.retry_count,
                    "timeout_seconds": st.timeout_seconds,
                    "dispatch_mode": st.dispatch_mode.value,
                    "dispatch_retry_count": st.dispatch_retry_count,
                    "agent_config": st.agent_config,
                    "steps": [s.to_dict() for s in st.steps],
                    "result": {
                        "subtask_id": st.result.subtask_id,
                        "worker_id": st.result.worker_id,
                        "modified_files": [
                            {
                                "path": fc.file_path,
                                "change_type": fc.change_type.value,
                                "diff_stats": fc.diff[:200] if fc.diff else "",
                            }
                            for fc in st.result.modified_files
                        ],
                        "summary": st.result.summary,
                        "success": st.result.success,
                        "completed_at": st.result.completed_at.isoformat(),
                        "adaptation_strategy": st.result.adaptation_strategy.value,
                        "stderr_tail": st.result.stderr_tail,
                        "recent_tool_calls": st.result.recent_tool_calls,
                        "retry_count": st.result.retry_count,
                        "error": st.result.error,
                        # T15 #660: a checkpoint is one more hop a collected
                        # number can die on. Without this, a worker that
                        # restarts and re-publishes a full snapshot (`partial=
                        # False`) would report the same subtask with its usage
                        # silently gone.
                        "usage": (
                            st.result.usage.to_dict() if st.result.usage else None
                        ),
                        # T16 #661: same hop, same reason as the block above —
                        # a checkpoint is a place a collected verdict can die.
                        "review": (
                            st.result.review.to_dict() if st.result.review else None
                        ),
                    } if st.result else None,
                }
                for st in self.subtasks
            ],
            "result": self.result,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Reconstruct a Task from a checkpoint dict (inverse of to_dict).

        ponytail: handles version 1 format; ignores unknown keys for forward compat.
        """
        task = cls(
            id=data.get("id", ""),
            description=data.get("description", ""),
            project_id=data.get("project_id", ""),
            status=TaskStatus(data["status"]) if "status" in data else TaskStatus.CREATED,
            result=data.get("result"),
        )
        if "created_at" in data:
            task.created_at = datetime.fromisoformat(data["created_at"])
        if "updated_at" in data:
            task.updated_at = datetime.fromisoformat(data["updated_at"])
        task.subtasks = []
        for sd in data.get("subtasks", []):
            st = Subtask(
                id=sd.get("id", ""),
                parent_id=sd.get("parent_id", ""),
                description=sd.get("description", ""),
                status=SubtaskStatus(sd["status"]) if "status" in sd else SubtaskStatus.PENDING,
                assigned_worker=sd.get("assigned_worker"),
                depends_on=sd.get("depends_on", []),
                priority=sd.get("priority", 0),
                file_constraints=sd.get("file_constraints", []),
                expected_output=sd.get("expected_output", ""),
                retry_count=sd.get("retry_count", 0),
                timeout_seconds=sd.get("timeout_seconds", 0),
                dispatch_mode=(
                    DispatchMode(sd["dispatch_mode"])
                    if "dispatch_mode" in sd
                    else DispatchMode.PREFER_REMOTE
                ),
                dispatch_retry_count=sd.get("dispatch_retry_count", 0),
                required_capabilities=sd.get("required_capabilities", []),
                agent_config=_resolve_agent_config_field(sd),
                steps=[WorkflowStep.from_dict(s) for s in sd.get("steps", [])],
            )
            rd = sd.get("result")
            if rd is not None:
                st.result = SubtaskResult(
                    subtask_id=rd.get("subtask_id", ""),
                    worker_id=rd.get("worker_id", ""),
                    summary=rd.get("summary", ""),
                    success=rd.get("success", True),
                    adaptation_strategy=AdaptationStrategy(rd.get("adaptation_strategy", "none")),
                    stderr_tail=rd.get("stderr_tail", ""),
                    recent_tool_calls=rd.get("recent_tool_calls", []),
                    retry_count=rd.get("retry_count", 0),
                    error=rd.get("error", ""),
                    # Absent key (every checkpoint written before T15, and any
                    # publisher that omits it) stays `None` — "not reported".
                    usage=(
                        SubtaskUsage.from_dict(rd["usage"])
                        if rd.get("usage") is not None
                        else None
                    ),
                    review=(
                        SubtaskReview.from_dict(rd["review"])
                        if rd.get("review") is not None
                        else None
                    ),
                )
                if "modified_files" in rd:
                    for fc in rd["modified_files"]:
                        st.result.modified_files.append(FileChange(
                            file_path=fc.get("path", ""),
                            change_type=ChangeType(fc.get("change_type", "modified")),
                            diff=fc.get("diff_stats", ""),
                        ))
                if "completed_at" in rd:
                    st.result.completed_at = datetime.fromisoformat(rd["completed_at"])
            task.subtasks.append(st)
        return task

    @property
    def is_complete(self) -> bool:
        """Whether all subtasks have completed successfully."""
        return (
            len(self.subtasks) > 0
            and all(st.is_complete for st in self.subtasks)
        )

    @property
    def has_failed(self) -> bool:
        """Whether any subtask has failed and cannot be retried."""
        return any(st.is_failed for st in self.subtasks)

    @property
    def ready_subtasks(self) -> list[Subtask]:
        """Subtasks that are pending and have all dependencies met."""
        completed_ids = {st.id for st in self.subtasks if st.is_complete}
        return [
            st for st in self.subtasks
            if st.is_ready and all(dep in completed_ids for dep in st.depends_on)
        ]


@dataclass
class WorkerInfo:
    """Information about a registered worker."""
    id: str = ""
    capabilities: list[str] = field(default_factory=list)
    current_load: int = 0
    max_capacity: int = 3
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_available(self) -> bool:
        """Whether the worker can accept more subtasks."""
        return self.current_load < self.max_capacity


@dataclass
class OrchestratorConfig:
    """Configuration for the Orchestrator."""
    max_subtasks: int = 10
    max_retries: int = 3
    heartbeat_timeout_seconds: int = 60
    subtask_timeout_seconds: int = 600  # 10 min default per subtask
    # LLM planning context budget (tokens). Tool-calling loop stops
    # gathering context when cumulative tokens approach this limit.
    planning_context_budget: int = 50000
    # Max tool-calling rounds for plan_task / ask
    planning_max_tool_rounds: int = 5
    # Max chars per tool result (truncated if exceeded)
    tool_result_max_chars: int = 2000


# ── Agent Loop Types ──────────────────────────────────────────────


class AgentEventType(Enum):
    """Event types emitted by the Orchestrator's agent loop."""
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    AGENT_ERROR = "agent_error"


@dataclass
class AgentEvent:
    """A single event from the agent loop."""
    type: AgentEventType
    turn: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunConfig:
    """Configuration for a single agent loop run."""
    max_turns: int = 5
    token_budget: int = 50000
    abort_event: asyncio.Event | None = None
    steering_queue: asyncio.Queue | None = None


@dataclass
class ExecutionSpec:
    """Structured output from plan_task() — an execution spec, not a design doc.

    Every choice is pre-made so an implementer can execute top-to-bottom
    with ZERO design decisions.
    """
    context: str = ""
    approach: list[str] = field(default_factory=list)
    critical_files: list[str] = field(default_factory=list)
    verification: str = ""
    assumptions: str = ""
    raw_text: str = ""  # fallback for sandbox decomposition
