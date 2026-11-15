"""The epistemic gate: decide whether knowledge may be read or promoted.

The knowledge layer must never conflate four questions, expressed here as the
principles the gate enforces:

A. Facts vs interpretations - only evidence/claims produced by an attempt may
   travel, and only as far as their recorded visibility allows.
B. Validation vs release - a passing validation is *necessary* but never
   *sufficient*: promotion to the graph or the repository also needs a
   successful, non-stale attempt and, for canonical knowledge, an integration
   receipt from the target ref.
C. Attempt-local vs long-term - attempt records are readable only inside the
   same subtask retry chain; graph records need the same graph identity;
   repository records need a matching scope and revision binding.
D. Storage authority vs projections - this module is pure logic. It performs no
   I/O, reads no environment and mutates nothing. :class:`evidence.EvidenceLedger`
   owns persistence and rollout staging, composes :class:`EpistemicGate` with
   ``UC_KNOWLEDGE_STAGE`` and only writes when a decision allows it.

Every denial is reported with one of the exported ``REASON_*`` constants. Call
sites must not invent inline strings because the recovery view, worker
manifests and the dashboard switch on this vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .model import (
    ClaimStatus,
    KnowledgeScope,
    RevisionBinding,
    ValidationResult,
    Visibility,
    normalize_claim_status,
    normalize_validation_result,
    normalize_visibility,
    visibility_rank,
)

__all__ = [
    "EpistemicGate",
    "GateDecision",
    "PromotionRequest",
    "REASON_ATTEMPT_CHAIN_REQUIRED",
    "REASON_ATTEMPT_NOT_SUCCESSFUL",
    "REASON_ATTEMPT_STALE",
    "REASON_CLAIM_TERMINAL",
    "REASON_CROSS_SCOPE_DENIED",
    "REASON_GRAPH_NOT_SHARED",
    "REASON_GRAPH_SCOPE_MISSING",
    "REASON_INTEGRATION_MISSING",
    "REASON_PUSH_UNKNOWN",
    "REASON_REVISION_MISMATCH",
    "REASON_STAGE_DISABLED",
    "REASON_TARGET_NOT_HIGHER",
    "REASON_VALIDATION_FAILED",
    "REASON_VALIDATION_INCONCLUSIVE",
    "REASON_VALIDATION_MISSING",
]

# ---------------------------------------------------------------------------
# Denial reason vocabulary (imported by the ledger, recovery view and worker)
# ---------------------------------------------------------------------------

REASON_TARGET_NOT_HIGHER = "target_not_higher"
REASON_ATTEMPT_NOT_SUCCESSFUL = "attempt_not_successful"
REASON_ATTEMPT_STALE = "attempt_stale"
REASON_VALIDATION_MISSING = "validation_missing"
REASON_VALIDATION_FAILED = "validation_failed"
REASON_VALIDATION_INCONCLUSIVE = "validation_inconclusive"
REASON_REVISION_MISMATCH = "revision_mismatch"
REASON_GRAPH_SCOPE_MISSING = "graph_scope_missing"
REASON_INTEGRATION_MISSING = "integration_missing"
REASON_PUSH_UNKNOWN = "push_unknown"
REASON_CLAIM_TERMINAL = "claim_terminal"
REASON_CROSS_SCOPE_DENIED = "cross_scope_denied"
REASON_GRAPH_NOT_SHARED = "graph_not_shared"
REASON_ATTEMPT_CHAIN_REQUIRED = "attempt_chain_required"
REASON_STAGE_DISABLED = "stage_disabled"

#: Every reason the gate (and the ledger's stage check) may emit, in the order
#: they are grouped above. Useful for tests and dashboard vocabularies.
ALL_REASON_CODES = (
    REASON_TARGET_NOT_HIGHER,
    REASON_ATTEMPT_NOT_SUCCESSFUL,
    REASON_ATTEMPT_STALE,
    REASON_VALIDATION_MISSING,
    REASON_VALIDATION_FAILED,
    REASON_VALIDATION_INCONCLUSIVE,
    REASON_REVISION_MISMATCH,
    REASON_GRAPH_SCOPE_MISSING,
    REASON_INTEGRATION_MISSING,
    REASON_PUSH_UNKNOWN,
    REASON_CLAIM_TERMINAL,
    REASON_CROSS_SCOPE_DENIED,
    REASON_GRAPH_NOT_SHARED,
    REASON_ATTEMPT_CHAIN_REQUIRED,
    REASON_STAGE_DISABLED,
)

_PUSH_UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe(values: Iterable[Any]) -> tuple:
    """Deduplicate reason codes while preserving their first-seen order."""

    seen = set()
    collected = []
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        collected.append(text)
    return tuple(collected)


def _scope(value: Any) -> KnowledgeScope:
    if isinstance(value, KnowledgeScope):
        return value
    if isinstance(value, Mapping):
        return KnowledgeScope.from_dict(value)
    raise TypeError("scope must be a KnowledgeScope or a mapping")


def _binding(value: Any) -> RevisionBinding | None:
    if value is None:
        return None
    if isinstance(value, RevisionBinding):
        return value
    if isinstance(value, Mapping):
        return RevisionBinding.from_dict(value)
    raise TypeError("binding must be a RevisionBinding or a mapping")


def _revisions_match(left: Any, right: Any) -> bool:
    """Exact or unambiguous short-SHA prefix match (mirrors RevisionBinding)."""

    left_text = _text(left)
    right_text = _text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    if len(left_text) < 7 or len(right_text) < 7:
        return False
    return left_text.startswith(right_text) or right_text.startswith(left_text)


# ---------------------------------------------------------------------------
# Decisions and requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """Log-safe outcome of one gate evaluation.

    ``reasons`` is an ordered, deduplicated tuple of ``REASON_*`` codes; an
    allowed decision always carries an empty tuple. ``visibility`` is the
    visibility the caller may treat the record as having: the record's own
    level for reads, the target level for an allowed promotion and the current
    level for a denied promotion.
    """

    allowed: bool
    visibility: Visibility
    reasons: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "visibility", normalize_visibility(self.visibility))
        object.__setattr__(self, "reasons", _dedupe(self.reasons))

    @property
    def denied(self) -> bool:
        """True when the gate refused the read/promotion."""

        return not self.allowed

    def to_dict(self) -> dict:
        """JSON-safe projection used in manifests and API payloads."""

        return {
            "allowed": self.allowed,
            "denied": self.denied,
            "visibility": self.visibility.value,
            "reasons": list(self.reasons),
        }

    def summary(self) -> dict:
        """Log-safe one-dict summary (no payload, claim text or evidence ids)."""

        return {
            "allowed": self.allowed,
            "denied": self.denied,
            "visibility": self.visibility.value,
            "reason_count": len(self.reasons),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class PromotionRequest:
    """Facts the ledger passes to :meth:`EpistemicGate.evaluate_promotion`.

    ``human_locked`` is recorded for callers/audit only: a human lock blocks
    automatic supersede decisions in the ledger, not a deliberate promotion.
    Likewise the gate never mutates a claim; it only reports.
    """

    claim_visibility: Visibility = Visibility.ATTEMPT
    target_visibility: Visibility = Visibility.REPOSITORY
    scope: KnowledgeScope = field(default_factory=KnowledgeScope)
    status: ClaimStatus = ClaimStatus.CANDIDATE
    attempt_succeeded: bool = False
    attempt_stale: bool = False
    validation_result: ValidationResult | None = None
    validation_revision: str = ""
    target_revision: str = ""
    integration: RevisionBinding | None = None
    push_status: str = ""
    human_locked: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_visibility", normalize_visibility(self.claim_visibility))
        object.__setattr__(self, "target_visibility", normalize_visibility(self.target_visibility))
        if not isinstance(self.scope, KnowledgeScope):
            object.__setattr__(self, "scope", _scope(self.scope))
        object.__setattr__(self, "status", normalize_claim_status(self.status))
        object.__setattr__(self, "attempt_succeeded", bool(self.attempt_succeeded))
        object.__setattr__(self, "attempt_stale", bool(self.attempt_stale))
        if self.validation_result is not None:
            object.__setattr__(
                self,
                "validation_result",
                normalize_validation_result(self.validation_result),
            )
        object.__setattr__(self, "validation_revision", _text(self.validation_revision))
        object.__setattr__(self, "target_revision", _text(self.target_revision))
        if self.integration is not None and not isinstance(
            self.integration, RevisionBinding
        ):
            object.__setattr__(self, "integration", _binding(self.integration))
        object.__setattr__(self, "push_status", _text(self.push_status))
        object.__setattr__(self, "human_locked", bool(self.human_locked))

    def to_dict(self) -> dict:
        """JSON-safe projection (claim texts never enter the gate)."""

        return {
            "claim_visibility": self.claim_visibility.value,
            "target_visibility": self.target_visibility.value,
            "scope": self.scope.to_dict(),
            "status": self.status.value,
            "attempt_succeeded": self.attempt_succeeded,
            "attempt_stale": self.attempt_stale,
            "validation_result": (
                self.validation_result.value if self.validation_result is not None else None
            ),
            "validation_revision": self.validation_revision,
            "target_revision": self.target_revision,
            "integration": self.integration.to_dict() if self.integration is not None else None,
            "push_status": self.push_status,
            "human_locked": self.human_locked,
        }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class EpistemicGate:
    """Pure read/promotion rules for the knowledge layer.

    The gate holds no state; every call receives the facts it needs. Ledger code
    composes it with rollout staging and persistence.
    """

    # -- reads -------------------------------------------------------------

    def can_read(
        self,
        *,
        record_visibility: Visibility,
        record_scope: KnowledgeScope,
        reader_visibility: Visibility | None = None,
        reader_scope: KnowledgeScope | None = None,
        binding: RevisionBinding | None = None,
        reader_revision: str = "",
        same_chain: bool = False,
    ) -> GateDecision:
        """Decide whether a reader may see a record at ``record_visibility``.

        Rules:

        * ATTEMPT records require ``same_chain=True`` (same subtask retry chain);
          the attempt identity itself is the only scope that matters.
        * GRAPH records require a tenant/repository/branch-compatible reader
          scope *and* a non-empty, matching ``graph_id`` on both sides, and a
          reader visibility of at least GRAPH.
        * REPOSITORY records require a compatible scope, a REPOSITORY-level
          reader, and a ``binding`` that applies to ``reader_revision`` (an
          unpinned binding is a wildcard; a pinned mismatch denies).

        ``reader_visibility`` defaults to the record visibility and
        ``reader_scope`` defaults to the record scope. ``binding=None`` means the
        record is not revision-bound.
        """

        record_visibility_value = normalize_visibility(record_visibility)
        reader_visibility_value = (
            record_visibility_value
            if reader_visibility is None
            else normalize_visibility(reader_visibility)
        )
        record_scope_value = _scope(record_scope)
        reader_scope_value = (
            record_scope_value if reader_scope is None else _scope(reader_scope)
        )
        reasons = []

        if record_visibility_value is Visibility.ATTEMPT:
            if not same_chain:
                reasons.append(REASON_ATTEMPT_CHAIN_REQUIRED)
        elif record_visibility_value is Visibility.GRAPH:
            if not record_scope_value.allows_read(reader_scope_value):
                reasons.append(REASON_CROSS_SCOPE_DENIED)
            if not record_scope_value.allows_read(reader_scope_value, require_graph=True):
                reasons.append(REASON_GRAPH_NOT_SHARED)
            if visibility_rank(reader_visibility_value) < visibility_rank(Visibility.GRAPH):
                reasons.append(REASON_CROSS_SCOPE_DENIED)
        else:  # Visibility.REPOSITORY
            if not record_scope_value.allows_read(reader_scope_value):
                reasons.append(REASON_CROSS_SCOPE_DENIED)
            if visibility_rank(reader_visibility_value) < visibility_rank(
                Visibility.REPOSITORY
            ):
                reasons.append(REASON_CROSS_SCOPE_DENIED)
            pinned = _binding(binding)
            if pinned is not None and not pinned.applies_to(reader_revision):
                reasons.append(REASON_REVISION_MISMATCH)

        allowed = not reasons
        return GateDecision(
            allowed=allowed,
            visibility=record_visibility_value,
            reasons=tuple(reasons),
        )

    # -- promotion ---------------------------------------------------------

    def evaluate_promotion(self, request: PromotionRequest) -> GateDecision:
        """Decide whether a claim may be promoted to ``target_visibility``.

        The order is deliberate (cheapest/first line first):

        1. superseded/retracted claims are terminal and can never be promoted;
        2. the target must be strictly higher than the current visibility;
        3. any promotion to GRAPH or REPOSITORY needs a successful, non-stale
           attempt, an explicit PASS validation, a matching validation revision
           and a non-empty graph scope;
        4. GRAPH -> REPOSITORY additionally needs an integration receipt
           (target ref + landed SHA), a push status that is not ``UNKNOWN`` and
           an integration revision that applies to the target revision.

        A denied decision keeps ``claim_visibility``; the gate never mutates.
        """

        claim_visibility = normalize_visibility(request.claim_visibility)
        target_visibility = normalize_visibility(request.target_visibility)
        reasons = []

        if request.status in (ClaimStatus.SUPERSEDED, ClaimStatus.RETRACTED):
            return GateDecision(
                allowed=False,
                visibility=claim_visibility,
                reasons=(REASON_CLAIM_TERMINAL,),
            )

        if visibility_rank(target_visibility) <= visibility_rank(claim_visibility):
            return GateDecision(
                allowed=False,
                visibility=claim_visibility,
                reasons=(REASON_TARGET_NOT_HIGHER,),
            )

        if visibility_rank(target_visibility) >= visibility_rank(Visibility.GRAPH):
            if not request.attempt_succeeded:
                reasons.append(REASON_ATTEMPT_NOT_SUCCESSFUL)
            if request.attempt_stale:
                reasons.append(REASON_ATTEMPT_STALE)
            if request.validation_result is None:
                reasons.append(REASON_VALIDATION_MISSING)
            elif request.validation_result is ValidationResult.FAIL:
                reasons.append(REASON_VALIDATION_FAILED)
            elif request.validation_result is ValidationResult.INCONCLUSIVE:
                reasons.append(REASON_VALIDATION_INCONCLUSIVE)
            if (
                request.validation_revision
                and request.target_revision
                and not _revisions_match(request.validation_revision, request.target_revision)
            ):
                reasons.append(REASON_REVISION_MISMATCH)
            if not request.scope.graph_id.strip():
                reasons.append(REASON_GRAPH_SCOPE_MISSING)

        if target_visibility is Visibility.REPOSITORY:
            integration = request.integration
            if integration is None or not integration.has_integration:
                reasons.append(REASON_INTEGRATION_MISSING)
            if _text(request.push_status).upper() == _PUSH_UNKNOWN:
                reasons.append(REASON_PUSH_UNKNOWN)
            if integration is not None and integration.revision.strip() and request.target_revision:
                if not _revisions_match(integration.revision, request.target_revision):
                    reasons.append(REASON_REVISION_MISMATCH)

        allowed = not reasons
        return GateDecision(
            allowed=allowed,
            visibility=target_visibility if allowed else claim_visibility,
            reasons=tuple(reasons),
        )
