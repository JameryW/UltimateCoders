"""Model routing: pick the model and reasoning budget by stage and risk, and
classify failures by whether another LLM call is worth making.

This closes the last gap row in ``docs/agent-os-architecture.md`` §5. Two
questions used to be answered by convention rather than by code:

* **Which model, with how much reasoning budget, should this call use?**
  Every stage used the one configured default: planning a decomposition and
  recovering from a rejected plan made the same request with the same budget.
  :class:`ModelRouter` answers from an explicit table keyed by (stage, risk),
  so a high-risk implementation can ask for a stronger model and a wider tool
  loop than a read-only review, and the choice is auditable per call.

* **Is another LLM call worth making after this failure?** A network timeout
  is worth an identical retry. A permission rejection is worth *no* retry --
  the gateway or an operator must act. A malformed request is worth one
  *reduced* retry after the request is repaired. :func:`classify_failure`
  turns a provider error string into one of those actions; it is a refinement
  of ``llm.py``'s transient/permanent split, which lumped schema errors in
  with auth errors and refused to retry either.

What this module deliberately does **not** do:

* **It does not call the LLM.** Routing is a pure function of (stage, risk,
  failure): no classifier model, no embedding, no network. The same inputs
  always produce the same decision, so the decision can be logged, tested and
  replayed.
* **It does not invent capability.** A route can only name a model the
  configured provider serves and a budget the executor accepts. Where an
  executor exposes no knob (the sandboxed coding agents own their tool loop
  in a subprocess), the decision is still *recorded* per attempt in the
  ``subtask_started`` / retry events -- the gap stays visible in the event
  stream instead of being papered over.
* **With no table configured, routing is the identity.** :meth:`route
  <ModelRouter.route>` returns an empty model and an empty budget, which
  every caller treats as "keep the configured default". The default path is
  one dict lookup per call and changes no behavior -- the same rule
  ``budget.py`` follows for unconfigured caps.

Stages follow the architecture doc's vocabulary. ``TOOL_SELECTION`` has no
separate call site in this repository: the sandbox agents run their own tool
loop in-process, and the orchestrator's planning loop carries its tools
inside ``PLANNING``. The enum value and table cell exist so a future split
needs no migration; nothing here queries it today.

The table is JSON in ``UC_ROUTE_TABLE``::

    {"implementation:high": {"model": "claude-sonnet-4-6", "max_tokens": 8192,
                             "max_tool_rounds": 10},
     "recovery:medium":   {"max_tokens": 2048}}

Keys are ``<stage>:<risk>``; values may carry ``model`` and any subset of
``max_tokens`` / ``max_tool_rounds`` / ``context_budget``. A malformed entry
is logged and ignored -- a typo must not silently reroute a fleet.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .llm import _TRANSIENT_RETRY_MARKERS

logger = logging.getLogger(__name__)

#: Env var holding the routing table as JSON (see the module docstring).
ENV_ROUTE_TABLE = "UC_ROUTE_TABLE"
#: Scale applied to a route's budget when a schema failure calls for a
#: lightweight repair instead of an identical retry (0.5 = half).
ENV_REPAIR_SCALE = "UC_ROUTE_REPAIR_SCALE"
DEFAULT_REPAIR_SCALE = 0.5

#: File constraints at or above this count make a subtask high risk: the
#: conflict surface is wide enough that a wrong merge is expensive.
HIGH_RISK_FILE_COUNT = 3


class Stage(Enum):
    """Where in the task lifecycle a call happens."""

    PLANNING = "planning"
    TOOL_SELECTION = "tool-selection"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    RECOVERY = "recovery"


class RiskLevel(Enum):
    """How much is at stake in the work, derived from its shape."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FailureKind(Enum):
    """What kind of failure a provider error is, for routing purposes."""

    TRANSIENT = "transient"  # transport/overload -- identical retry is fair
    AUTH = "auth"  # credentials/permissions -- the gateway must act
    SCHEMA = "schema"  # malformed request -- one reduced retry after repair
    UNKNOWN = "unknown"  # unclassified -- retry, gated by the evidence check


# ── risk derivation ──────────────────────────────────────────────

#: Description keywords marking work whose failure is expensive to undo.
_HIGH_RISK_KEYWORDS = (
    "deploy",
    "release",
    "publish",
    "ship",
    "migrate",
    "rollback",
    "delete",
    "drop",
    "purge",
    "rm -rf",
    "production",
)
#: Keywords marking work that writes code.
_WRITE_KEYWORDS = (
    "fix",
    "bug",
    "patch",
    "repair",
    "resolve error",
    "implement",
    "refactor",
    "restructure",
    "reorganize",
    "clean up",
    "update",
    "add",
    "create",
    "write",
    "modify",
    "change",
)
#: Keywords marking read-only work.
_READ_ONLY_KEYWORDS = (
    "review",
    "audit",
    "analyze",
    "analyse",
    "inspect",
    "search",
    "find",
    "locate",
    "grep",
    "document",
    "docs",
    "readme",
    "explain",
    "summarize",
    "summarise",
)


def derive_risk(
    description: str,
    file_constraints: Any = (),
    capabilities: Any = (),
) -> str:
    """Risk level for one unit of work, from its shape -- never from a model.

    Order matters and each step is a fact about the work, not a guess about
    its outcome:

    1. A declared read-only capability is enforced (the review template bans
       Edit/Write), so it outranks every keyword: reviewing a deploy script
       is still read-only work.
    2. Dangerous keywords or a wide conflict surface (>= 3 constrained files)
       make the work high risk -- a wrong result there is expensive to undo.
    3. Read-only keywords without any write keyword make it low risk.
    4. Everything else is medium: writes, or a shape nothing recognised.
       Unknown work is not assumed cheap -- routing it at the cheap model is
       the one direction that cannot be walked back after the fact.
    """
    text = str(description or "").lower()
    caps = {str(c).lower() for c in (capabilities or ())}
    paths = [p for p in (file_constraints or ()) if p]
    if "review" in caps or "search" in caps:
        return RiskLevel.LOW.value
    if any(kw in text for kw in _HIGH_RISK_KEYWORDS):
        return RiskLevel.HIGH.value
    if len(paths) >= HIGH_RISK_FILE_COUNT:
        return RiskLevel.HIGH.value
    if any(kw in text for kw in _READ_ONLY_KEYWORDS) and not any(
        kw in text for kw in _WRITE_KEYWORDS
    ):
        return RiskLevel.LOW.value
    return RiskLevel.MEDIUM.value


# ── reasoning budget ─────────────────────────────────────────────


@dataclass(frozen=True)
class ReasoningBudget:
    """Per-call reasoning limits. Every field optional; ``None`` = default.

    ``max_tokens`` bounds one response, ``max_tool_rounds`` bounds the
    tool-calling loop, ``context_budget`` bounds prompt assembly. A field
    left ``None`` means "keep whatever the caller already uses" -- a route
    never has to restate the whole configuration to change one dimension.
    """

    max_tokens: int | None = None
    max_tool_rounds: int | None = None
    context_budget: int | None = None

    def is_identity(self) -> bool:
        return (
            self.max_tokens is None
            and self.max_tool_rounds is None
            and self.context_budget is None
        )

    def shrink(self, scale: float) -> ReasoningBudget:
        """Scale every set field down, for a lightweight repair retry.

        An identity budget shrinks to itself: there is nothing to reduce,
        and the repair then relies on the request change (the rejection
        reason) plus the evidence gate rather than on a smaller allowance.
        """
        if self.is_identity() or scale >= 1.0:
            return self

        def _scale(value: int | None) -> int | None:
            return None if value is None else max(1, int(round(value * scale)))

        return ReasoningBudget(
            max_tokens=_scale(self.max_tokens),
            max_tool_rounds=_scale(self.max_tool_rounds),
            context_budget=_scale(self.context_budget),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_tool_rounds": self.max_tool_rounds,
            "context_budget": self.context_budget,
        }


@dataclass(frozen=True)
class RouteDecision:
    """What one call should use, and why -- safe to log and to diff."""

    stage: str
    risk: str
    #: Empty means "keep the caller's configured model".
    model: str = ""
    budget: ReasoningBudget = ReasoningBudget()
    #: ``table`` (a configured cell), ``identity`` (nothing configured), or
    #: the repair explanation.
    reason: str = "identity"

    @property
    def is_identity(self) -> bool:
        return not self.model and self.budget.is_identity()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "risk": self.risk,
            "model": self.model,
            "budget": self.budget.to_dict(),
            "reason": self.reason,
        }


# ── routing table ────────────────────────────────────────────────


@dataclass(frozen=True)
class RouteCell:
    """One table entry: a model and/or a budget for a (stage, risk) pair."""

    model: str = ""
    budget: ReasoningBudget = ReasoningBudget()


class RoutingTable:
    """(stage, risk) -> RouteCell. An empty table routes everything to identity."""

    def __init__(self, cells: dict[tuple[str, str], RouteCell] | None = None) -> None:
        self._cells = dict(cells or {})

    @classmethod
    def from_env(cls, raw: str | None = None) -> RoutingTable:
        """Parse ``UC_ROUTE_TABLE``. A malformed table logs and routes identity."""
        if raw is None:
            raw = os.environ.get(ENV_ROUTE_TABLE, "")
        text = str(raw or "").strip()
        if not text:
            return cls()
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            logger.warning("%s is not valid JSON; model routing disabled", ENV_ROUTE_TABLE)
            return cls()
        if not isinstance(parsed, dict):
            logger.warning("%s is not a JSON object; model routing disabled", ENV_ROUTE_TABLE)
            return cls()
        stages = {s.value for s in Stage}
        risks = {r.value for r in RiskLevel}
        cells: dict[tuple[str, str], RouteCell] = {}
        for key, value in parsed.items():
            stage, _, risk = str(key).partition(":")
            stage, risk = stage.strip().lower(), risk.strip().lower()
            if stage not in stages or risk not in risks:
                logger.warning(
                    "%s entry %r is not <stage>:<risk> with known names; ignored",
                    ENV_ROUTE_TABLE,
                    key,
                )
                continue
            if not isinstance(value, dict):
                logger.warning("%s entry %r is not an object; ignored", ENV_ROUTE_TABLE, key)
                continue
            cells[(stage, risk)] = RouteCell(
                model=str(value.get("model") or ""),
                budget=ReasoningBudget(
                    max_tokens=_positive_int(value.get("max_tokens")),
                    max_tool_rounds=_positive_int(value.get("max_tool_rounds")),
                    context_budget=_positive_int(value.get("context_budget")),
                ),
            )
        return cls(cells)

    def cell(self, stage: Any, risk: Any) -> RouteCell:
        key = (
            stage.value if isinstance(stage, Stage) else str(stage),
            risk.value if isinstance(risk, RiskLevel) else str(risk),
        )
        return self._cells.get(key, RouteCell())

    def __bool__(self) -> bool:
        return bool(self._cells)

    def __len__(self) -> int:
        return len(self._cells)


def _positive_int(value: Any) -> int | None:
    """A positive int, or None. A malformed budget is ignored, not clamped to 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _repair_scale_from_env() -> float:
    raw = os.environ.get(ENV_REPAIR_SCALE)
    if raw is None or not str(raw).strip():
        return DEFAULT_REPAIR_SCALE
    try:
        scale = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a number; using %s", ENV_REPAIR_SCALE, raw, DEFAULT_REPAIR_SCALE
        )
        return DEFAULT_REPAIR_SCALE
    if not 0.0 < scale < 1.0:
        logger.warning(
            "%s=%r is not in (0, 1); using %s", ENV_REPAIR_SCALE, raw, DEFAULT_REPAIR_SCALE
        )
        return DEFAULT_REPAIR_SCALE
    return scale


# ── failure classification ───────────────────────────────────────

#: Transport failures that llm.py already backs off on, plus the socket-level
#: spellings that surface before an HTTP status exists. Worth an identical
#: retry: the request was fine, the pipe was not.
_TRANSPORT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "econnreset",
    "econnrefused",
    "ssl",
    "tls",
    "unreachable",
    "temporarily unavailable",
    "peer reset",
    "broken pipe",
)
#: Credentials / permission / endpoint-config failures -- the subset of
#: llm.py's permanent markers that no retry can fix. "not found" belongs
#: here too: in an LLM error it means the endpoint or model name is wrong,
#: which is configuration, not a malformed body.
_AUTH_MARKERS = (
    "401",
    "403",
    "404",
    "invalid_api_key",
    "invalid key",
    "authentication",
    "unauthorized",
    "forbidden",
    "permission denied",
    "access denied",
    "not signed in",
    "login --device-code",
    "xai_api_key",
    "not found",
)
#: Malformed-request failures -- the rest of llm.py's permanent markers plus
#: the request-shape errors that arrive without a status line. Worth ONE
#: reduced retry after the request is repaired: re-sending the identical
#: body fails identically, but a trimmed prompt or a dropped tool often
#: clears it.
_SCHEMA_MARKERS = (
    "400",
    "bad request",
    "invalid request",
    "malformed",
    "json",
    "validation",
    "schema",
    "context length",
    "too long",
    "max_tokens",
    "parameter",
    "unsupported",
    "tool_use",
    "tool call",
    "decode",
)


@dataclass(frozen=True)
class FailureAction:
    """Whether another LLM call is worth making, and what kind it should be."""

    kind: str  # FailureKind value
    #: ``retry`` (identical), ``lightweight_repair`` (reduced, after repair)
    #: or ``escalate_gateway`` (none -- a human/gateway must act).
    action: str
    worth_another_call: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action": self.action,
            "worth_another_call": self.worth_another_call,
            "reason": self.reason,
        }


def classify_failure(error: Any) -> FailureAction:
    """Classify a provider error by what another call could achieve.

    Order is transient, auth, schema, unknown -- the same precedence llm.py
    uses, so a message that could match two vocabularies lands on the more
    actionable one (a 429 is a retry even though its text may mention a
    schema-ish parameter).

    ``UNKNOWN`` maps to ``retry`` with ``worth_another_call=True``: that is
    today's behavior (everything not permanent retried), and the evidence
    gate -- the progress detector's stall check -- is what bounds it, not
    this classifier pretending to know more than it does.
    """
    low = str(error or "").strip().lower()
    if not low:
        return FailureAction(FailureKind.UNKNOWN.value, "abstain", False, "empty error text")
    if any(m in low for m in _TRANSIENT_RETRY_MARKERS) or any(m in low for m in _TRANSPORT_MARKERS):
        return FailureAction(
            FailureKind.TRANSIENT.value,
            "retry",
            True,
            "transport or overload: the request was sound, an identical retry is fair",
        )
    if any(m in low for m in _AUTH_MARKERS):
        return FailureAction(
            FailureKind.AUTH.value,
            "escalate_gateway",
            False,
            "credentials, permissions or endpoint configuration: no LLM retry can fix it",
        )
    if any(m in low for m in _SCHEMA_MARKERS):
        return FailureAction(
            FailureKind.SCHEMA.value,
            "lightweight_repair",
            True,
            "malformed request: worth one reduced retry after the request is repaired",
        )
    return FailureAction(
        FailureKind.UNKNOWN.value,
        "retry",
        True,
        "unclassified: retry, bounded by the evidence gate",
    )


# ── the router ───────────────────────────────────────────────────


class ModelRouter:
    """Deterministic (stage, risk, failure) -> :class:`RouteDecision`."""

    def __init__(
        self,
        table: RoutingTable | None = None,
        *,
        repair_scale: float | None = None,
    ) -> None:
        self.table = table if table is not None else RoutingTable.from_env()
        self.repair_scale = repair_scale if repair_scale is not None else _repair_scale_from_env()

    def route(
        self,
        stage: Any,
        risk: Any,
        *,
        failure: str = "",
        base_stage: Any = None,
    ) -> RouteDecision:
        """The decision for one call.

        A schema failure escalates the stage to ``RECOVERY`` and reduces the
        budget: the retry must not be the identical request that just failed.
        An explicitly configured ``recovery`` cell is honored as written;
        without one, the repair budget is ``base_stage``'s own cell shrunk by
        the repair scale -- a retry that spends what the failed call spent is
        not a repair. ``base_stage`` defaults to ``stage``; pass the work's
        ordinary stage when ``stage`` is already ``RECOVERY`` (a retry), so
        the reduction is derived from the work's cell rather than from the
        recovery cell that may not exist. Every other failure kind leaves the
        (stage, risk) cell untouched -- a transport failure is not a reason
        to change models, and an auth failure produces no call at all.
        """
        stage_value = stage.value if isinstance(stage, Stage) else str(stage)
        risk_value = risk.value if isinstance(risk, RiskLevel) else str(risk)
        base_value = (
            base_stage.value if isinstance(base_stage, Stage) else str(base_stage)
        ) if base_stage is not None else stage_value
        action = classify_failure(failure) if str(failure or "").strip() else None
        if action is not None and action.kind == FailureKind.SCHEMA.value:
            recovery = self.table.cell(Stage.RECOVERY.value, risk_value)
            if recovery.model or not recovery.budget.is_identity():
                return RouteDecision(
                    stage=Stage.RECOVERY.value,
                    risk=risk_value,
                    model=recovery.model,
                    budget=recovery.budget,
                    reason="table",
                )
            base = self.table.cell(base_value, risk_value)
            return RouteDecision(
                stage=Stage.RECOVERY.value,
                risk=risk_value,
                model=base.model,
                budget=base.budget.shrink(self.repair_scale),
                reason=(
                    f"lightweight_repair: schema failure escalated to recovery "
                    f"at scale {self.repair_scale:g}"
                ),
            )
        cell = self.table.cell(stage_value, risk_value)
        configured = bool(cell.model) or not cell.budget.is_identity()
        return RouteDecision(
            stage=stage_value,
            risk=risk_value,
            model=cell.model,
            budget=cell.budget,
            reason="table" if configured else "identity",
        )

    def route_subtask(
        self,
        description: str,
        *,
        attempt: int = 0,
        last_failure: str = "",
        file_constraints: Any = (),
        capabilities: Any = (),
    ) -> RouteDecision:
        """The decision for one worker attempt of one subtask.

        The stage follows the attempt's role: a retry is recovery, a
        review-capability subtask is verification, anything else is
        implementation. The repair budget is always derived from the work's
        ordinary stage (``base_stage``), never from the recovery cell, so a
        second consecutive schema failure reduces from the same original
        budget instead of compounding. Planning happens in the orchestrator
        (it has no Subtask yet), which calls :meth:`route` directly.
        """
        risk = derive_risk(description, file_constraints, capabilities)
        caps = {str(c).lower() for c in (capabilities or ())}
        ordinary: Stage = Stage.VERIFICATION if "review" in caps else Stage.IMPLEMENTATION
        stage: Stage = Stage.RECOVERY if attempt > 0 else ordinary
        return self.route(stage, risk, failure=last_failure, base_stage=ordinary)
