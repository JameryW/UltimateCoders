"""Evidence, claims and validations: the factual layer under the epistemic gate.

The V2.0 knowledge upgrade separates three things the old memory path mixed:

* **evidence** - what an attempt actually observed, with a kind, a trust class
  and a bounded machine-safe payload (never a prompt, never an unbounded diff);
* **claims** - the interpretation extracted from evidence, attempt-scoped by
  default and only shared after an explicit gate decision;
* **validations** - the outcome of one validation strategy against one claim
  revision, kept as an append history instead of overwriting the claim.

:class:`EvidenceLedger` is the only writer. It owns ``RuntimeState`` I/O, rollout
staging (``UC_KNOWLEDGE_STAGE``) and the compare-and-set repairs that make
promotion atomic. :class:`ultimate_coders.knowledge.gate.EpistemicGate` remains
pure logic; this module composes it and turns "allowed" into exactly one
mutation, or none at all.

Invariants:

* Evidence is immutable and idempotent by ``evidence_id``; replay returns the
  stored record instead of duplicating it.
* Claims are deterministic by ``(scope, normalised statement)``; replay returns
  the existing claim and never overwrites it.
* Every promotion re-evaluates the gate inside the write transaction and aborts
  the transaction on denial, so a stale read cannot promote a claim.
* Denial reasons are the exported ``REASON_*`` constants from ``gate`` - never
  inline strings at call sites.
* Payloads are JSON-safe, bounded and redacted: prompt/chat/diff content is
  replaced by a digest + size marker before it can reach the state store.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..runtime_state import RecordConflictError, RuntimeState
from .gate import (
    REASON_STAGE_DISABLED,
    EpistemicGate,
    GateDecision,
    PromotionRequest,
)
from .model import (
    AttemptRef,
    ClaimStatus,
    EvidenceKind,
    KnowledgeScope,
    RevisionBinding,
    ValidationResult,
    Visibility,
    canonical_json,
    default_trust_class,
    default_verified,
    normalize_claim_status,
    normalize_evidence_kind,
    normalize_validation_result,
    normalize_visibility,
    safe_text,
    short_digest,
    utc_now_iso,
    visibility_rank,
)
from .rollout import KnowledgeStage, resolve_stage, stage_from_env

__all__ = [
    "CLAIM_NAMESPACE",
    "NAMESPACE",
    "ClaimRecord",
    "EvidenceLedger",
    "EvidenceRecord",
    "ValidationRecord",
]

NAMESPACE = "knowledge_evidence"
CLAIM_NAMESPACE = "knowledge_claims"

#: Hard bounds that keep every stored record log-safe and PostgreSQL-friendly.
MAX_VALIDATIONS = 20
MAX_PAYLOAD_CHARS = 8_000
MAX_PAYLOAD_DEPTH = 3
MAX_PAYLOAD_ITEMS = 50
MAX_PAYLOAD_STRING = 2_000
MAX_SUMMARY_CHARS = 500
MAX_CLAIM_STATEMENT_CHARS = 2_000
MAX_LIFECYCLE_REASON_CHARS = 1_000
MAX_ID_CHARS = 120
MAX_EVIDENCE_IDS = 50
CAS_RETRIES = 3

_TERMINAL_STATUSES = (ClaimStatus.SUPERSEDED, ClaimStatus.RETRACTED)
#: Keys whose values are prompt/transcript/diff material. Their contents never
#: enter evidence payloads; a digest and a size marker preserve comparability.
_REDACTED_PAYLOAD_KEYS = frozenset(
    {
        "prompt",
        "system_prompt",
        "messages",
        "chat_history",
        "transcript",
        "diff",
        "patch",
    }
)
_PUSH_UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Small coercion / bounding helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value or "").strip()


def _attempt_ref(value: Any) -> AttemptRef:
    if isinstance(value, AttemptRef):
        return value
    if isinstance(value, Mapping):
        return AttemptRef.from_dict(value)
    raise TypeError("attempt must be an AttemptRef or a mapping")


def _scope_of(value: Any) -> KnowledgeScope:
    if isinstance(value, KnowledgeScope):
        return value
    if isinstance(value, Mapping):
        return KnowledgeScope.from_dict(value)
    raise TypeError("scope must be a KnowledgeScope or a mapping")


def _binding_of(value: Any) -> RevisionBinding:
    if value is None:
        return RevisionBinding()
    if isinstance(value, RevisionBinding):
        return value
    if isinstance(value, Mapping):
        return RevisionBinding.from_dict(value)
    raise TypeError("binding must be a RevisionBinding or a mapping")


def _evidence_kind_value(value: Any) -> EvidenceKind:
    if value is None or str(value).strip() == "":
        return EvidenceKind.HYPOTHESIS
    return normalize_evidence_kind(value)


def _claim_status_value(value: Any) -> ClaimStatus:
    if value is None or str(value).strip() == "":
        return ClaimStatus.CANDIDATE
    return normalize_claim_status(value)


def _validation_result_value(value: Any) -> ValidationResult:
    if value is None or str(value).strip() == "":
        return ValidationResult.INCONCLUSIVE
    return normalize_validation_result(value)


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(0, min(maximum, number))


def _dedupe_ids(values: Any) -> tuple:
    """Bounded, order-preserving, duplicate-free tuple of evidence ids."""

    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    collected = []
    seen = set()
    for item in values:
        text = safe_text(item, limit=MAX_ID_CHARS).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        collected.append(text)
        if len(collected) >= MAX_EVIDENCE_IDS:
            break
    return tuple(collected)


def _normalize_statement(statement: Any) -> str:
    """Collapse whitespace for the claim key and its stored display text."""

    text = " ".join(safe_text(statement, limit=MAX_CLAIM_STATEMENT_CHARS).split())
    if not text:
        raise ValueError("claim statement is required")
    return text


def _redacted(value: Any) -> dict:
    """Replace prompt/diff material with a comparable, non-recoverable marker."""

    rendered = canonical_json(value)
    return {
        "redacted": True,
        "chars": len(rendered),
        "digest": short_digest(rendered),
    }


def _collapsed(value: Any) -> str:
    return safe_text(canonical_json(value), limit=MAX_PAYLOAD_STRING)


def _bounded_value(value: Any, depth: int) -> Any:
    """Bound one payload value; ``depth`` is the container nesting level."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return safe_text(value, limit=32)
        return value
    if isinstance(value, str):
        return safe_text(value, limit=MAX_PAYLOAD_STRING)
    if isinstance(value, Mapping):
        if depth >= MAX_PAYLOAD_DEPTH:
            return _collapsed(value)
        return _bounded_mapping(value, depth + 1)
    if isinstance(value, (list, tuple)):
        if depth >= MAX_PAYLOAD_DEPTH:
            return _collapsed(value)
        return [_bounded_value(item, depth + 1) for item in list(value)[:MAX_PAYLOAD_ITEMS]]
    if isinstance(value, (set, frozenset)):
        if depth >= MAX_PAYLOAD_DEPTH:
            return _collapsed(value)
        items = sorted((_bounded_value(item, depth + 1) for item in value), key=canonical_json)
        return items[:MAX_PAYLOAD_ITEMS]
    return safe_text(value, limit=MAX_PAYLOAD_STRING)


def _bounded_mapping(value: Mapping, depth: int) -> dict:
    bounded = {}
    for key, item in value.items():
        text = safe_text(key, limit=80).strip() or "field"
        if text.lower() in _REDACTED_PAYLOAD_KEYS:
            bounded[text] = _redacted(item)
            continue
        bounded[text] = _bounded_value(item, depth)
    return bounded


def _bounded_payload(payload: Any) -> dict:
    """JSON-safe, bounded, redacted evidence payload.

    Nested mappings collapse past :data:`MAX_PAYLOAD_DEPTH`; long strings are
    clipped; prompt/transcript/diff keys are redacted; and the whole payload is
    replaced by a digest envelope when the canonical form exceeds
    :data:`MAX_PAYLOAD_CHARS`.
    """

    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        bounded = _bounded_mapping(payload, 0)
    else:
        bounded = {"value": _bounded_value(payload, 0)}
    rendered = canonical_json(bounded)
    if len(rendered) > MAX_PAYLOAD_CHARS:
        return {
            "truncated": True,
            "chars": len(rendered),
            "payload_digest": short_digest(bounded),
            "preview": safe_text(rendered, limit=1_000),
        }
    return bounded


def _evidence_digest(
    kind: EvidenceKind,
    attempt: AttemptRef,
    revision: RevisionBinding,
    payload: dict,
) -> str:
    return short_digest(
        {
            "kind": kind.value,
            "attempt": attempt.key(),
            "revision": revision.to_dict(),
            "payload": payload,
        }
    )


def _validation_digest(
    claim_id: str,
    result: ValidationResult,
    revision: str,
    evidence_ids: tuple,
    strategy: str,
    validator: str,
) -> str:
    return short_digest(
        {
            "claim_id": claim_id,
            "result": result.value,
            "revision": revision,
            "evidence_ids": list(evidence_ids),
            "strategy": strategy,
            "validator": validator,
        }
    )


def _find_validation(claim: ClaimRecord, validation_id: str) -> ValidationRecord | None:
    for validation in reversed(claim.validations):
        if validation.validation_id == validation_id:
            return validation
    return None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
    """One bounded observation made by one attempt.

    ``key()`` is the attempt-scoped identity used in logs and recovery views;
    the record is stored under ``evidence_id`` so a caller holding only the id
    can fetch it directly.
    """

    evidence_id: str
    kind: EvidenceKind
    attempt: AttemptRef
    scope: KnowledgeScope
    visibility: Visibility = Visibility.ATTEMPT
    revision: RevisionBinding = field(default_factory=RevisionBinding)
    payload: dict = field(default_factory=dict)
    summary: str = ""
    trust_class: str = ""
    verified: bool = False
    created_at: str = ""
    record_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", safe_text(self.evidence_id, limit=MAX_ID_CHARS))
        object.__setattr__(self, "kind", _evidence_kind_value(self.kind))
        object.__setattr__(self, "attempt", _attempt_ref(self.attempt))
        object.__setattr__(self, "scope", _scope_of(self.scope))
        object.__setattr__(self, "visibility", normalize_visibility(self.visibility))
        object.__setattr__(self, "revision", _binding_of(self.revision))
        object.__setattr__(
            self,
            "payload",
            dict(self.payload) if isinstance(self.payload, Mapping) else {},
        )
        object.__setattr__(self, "summary", safe_text(self.summary, limit=MAX_SUMMARY_CHARS))
        object.__setattr__(self, "trust_class", safe_text(self.trust_class, limit=MAX_ID_CHARS))
        object.__setattr__(self, "verified", bool(self.verified))
        object.__setattr__(self, "created_at", safe_text(self.created_at, limit=40))
        object.__setattr__(self, "record_version", max(0, _int(self.record_version)))

    def key(self) -> str:
        """Attempt-scoped identity, e.g. ``task:sub:0:1:<evidence_id>``."""

        return f"{self.attempt.key()}:{self.evidence_id}"

    def tokens(self) -> int:
        """Rough token heuristic used by recovery-view budgets."""

        body = canonical_json(self.payload) if self.payload else ""
        size = len(body) + len(self.summary)
        if size <= 0:
            return 0
        return max(1, (size + 3) // 4)

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "attempt": self.attempt.to_dict(),
            "scope": self.scope.to_dict(),
            "visibility": self.visibility.value,
            "revision": self.revision.to_dict(),
            "payload": dict(self.payload),
            "summary": self.summary,
            "trust_class": self.trust_class,
            "verified": self.verified,
            "created_at": self.created_at,
            "record_version": self.record_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceRecord:
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            evidence_id=_text(data.get("evidence_id")),
            kind=_evidence_kind_value(data.get("kind")),
            attempt=_attempt_ref(data.get("attempt") or {}),
            scope=_scope_of(data.get("scope") or {}),
            visibility=normalize_visibility(data.get("visibility")),
            revision=_binding_of(data.get("revision")),
            payload=dict(data.get("payload") or {}),
            summary=safe_text(data.get("summary"), limit=MAX_SUMMARY_CHARS),
            trust_class=safe_text(data.get("trust_class"), limit=MAX_ID_CHARS),
            verified=bool(data.get("verified", False)),
            created_at=safe_text(data.get("created_at"), limit=40),
            record_version=max(0, _int(data.get("record_version"))),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: no payload contents, only identity and bounds."""

        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "attempt_key": self.attempt.key(),
            "visibility": self.visibility.value,
            "revision": self.revision.revision,
            "trust_class": self.trust_class,
            "verified": self.verified,
            "summary": safe_text(self.summary, limit=200),
            "created_at": self.created_at,
            "record_version": self.record_version,
        }


@dataclass(frozen=True)
class ValidationRecord:
    """One validation outcome for one claim revision."""

    validation_id: str
    claim_id: str = ""
    result: ValidationResult = ValidationResult.INCONCLUSIVE
    revision: str = ""
    evidence_ids: tuple = ()
    strategy: str = ""
    validator: str = ""
    notes: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "validation_id", safe_text(self.validation_id, limit=MAX_ID_CHARS))
        object.__setattr__(self, "claim_id", safe_text(self.claim_id, limit=MAX_ID_CHARS))
        object.__setattr__(self, "result", _validation_result_value(self.result))
        object.__setattr__(self, "revision", safe_text(self.revision, limit=MAX_ID_CHARS).strip())
        object.__setattr__(self, "evidence_ids", _dedupe_ids(self.evidence_ids))
        object.__setattr__(self, "strategy", safe_text(self.strategy, limit=MAX_ID_CHARS).strip())
        object.__setattr__(self, "validator", safe_text(self.validator, limit=MAX_ID_CHARS).strip())
        object.__setattr__(self, "notes", safe_text(self.notes, limit=MAX_LIFECYCLE_REASON_CHARS))
        object.__setattr__(self, "created_at", safe_text(self.created_at, limit=40))

    def to_dict(self) -> dict:
        return {
            "validation_id": self.validation_id,
            "claim_id": self.claim_id,
            "result": self.result.value,
            "revision": self.revision,
            "evidence_ids": list(self.evidence_ids),
            "strategy": self.strategy,
            "validator": self.validator,
            "notes": self.notes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ValidationRecord:
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            validation_id=_text(data.get("validation_id")),
            claim_id=_text(data.get("claim_id")),
            result=_validation_result_value(data.get("result")),
            revision=_text(data.get("revision")),
            evidence_ids=data.get("evidence_ids") or (),
            strategy=_text(data.get("strategy")),
            validator=_text(data.get("validator")),
            notes=safe_text(data.get("notes"), limit=MAX_LIFECYCLE_REASON_CHARS),
            created_at=safe_text(data.get("created_at"), limit=40),
        )

    def to_summary(self) -> dict:
        """Log-safe projection (notes are bounded but omitted from logs)."""

        return {
            "validation_id": self.validation_id,
            "claim_id": self.claim_id,
            "result": self.result.value,
            "revision": self.revision,
            "strategy": self.strategy,
            "validator": self.validator,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ClaimRecord:
    """A knowledge claim with its lifecycle, evidence links and validations."""

    claim_id: str
    statement: str
    scope: KnowledgeScope
    visibility: Visibility = Visibility.ATTEMPT
    status: ClaimStatus = ClaimStatus.CANDIDATE
    attempt: AttemptRef | None = None
    evidence_ids: tuple = ()
    validations: tuple = ()
    binding: RevisionBinding = field(default_factory=RevisionBinding)
    confidence: float = 0.0
    human_locked: bool = False
    superseded_by: str = ""
    retract_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    record_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", safe_text(self.claim_id, limit=MAX_ID_CHARS))
        object.__setattr__(
            self,
            "statement",
            safe_text(self.statement, limit=MAX_CLAIM_STATEMENT_CHARS),
        )
        object.__setattr__(self, "scope", _scope_of(self.scope))
        object.__setattr__(self, "visibility", normalize_visibility(self.visibility))
        object.__setattr__(self, "status", _claim_status_value(self.status))
        if self.attempt is not None:
            object.__setattr__(self, "attempt", _attempt_ref(self.attempt))
        object.__setattr__(self, "evidence_ids", _dedupe_ids(self.evidence_ids))
        object.__setattr__(
            self,
            "validations",
            tuple(
                item if isinstance(item, ValidationRecord) else ValidationRecord.from_dict(item)
                for item in (self.validations or ())
                if isinstance(item, (ValidationRecord, Mapping))
            ),
        )
        object.__setattr__(self, "binding", _binding_of(self.binding))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "human_locked", bool(self.human_locked))
        object.__setattr__(self, "superseded_by", safe_text(self.superseded_by, limit=MAX_ID_CHARS))
        object.__setattr__(
            self,
            "retract_reason",
            safe_text(self.retract_reason, limit=MAX_LIFECYCLE_REASON_CHARS),
        )
        object.__setattr__(self, "created_at", safe_text(self.created_at, limit=40))
        object.__setattr__(self, "updated_at", safe_text(self.updated_at, limit=40))
        object.__setattr__(self, "record_version", max(0, _int(self.record_version)))

    def is_terminal(self) -> bool:
        """True for superseded/retracted claims (never promoted again)."""

        return self.status in _TERMINAL_STATUSES

    def latest_validation(self) -> ValidationRecord | None:
        """Most recent validation, or ``None`` when the claim is unvalidated."""

        if not self.validations:
            return None
        return self.validations[-1]

    @property
    def promoted_visibility(self) -> Visibility | None:
        """Visibility only when the claim is actually promoted, else ``None``."""

        return self.visibility if self.status is ClaimStatus.PROMOTED else None

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "scope": self.scope.to_dict(),
            "visibility": self.visibility.value,
            "status": self.status.value,
            "attempt": self.attempt.to_dict() if self.attempt is not None else None,
            "evidence_ids": list(self.evidence_ids),
            "validations": [validation.to_dict() for validation in self.validations],
            "binding": self.binding.to_dict(),
            "confidence": self.confidence,
            "human_locked": self.human_locked,
            "superseded_by": self.superseded_by,
            "retract_reason": self.retract_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "record_version": self.record_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ClaimRecord:
        data = payload if isinstance(payload, Mapping) else {}
        attempt_payload = data.get("attempt")
        validations = data.get("validations") or ()
        return cls(
            claim_id=_text(data.get("claim_id")),
            statement=safe_text(data.get("statement"), limit=MAX_CLAIM_STATEMENT_CHARS),
            scope=_scope_of(data.get("scope") or {}),
            visibility=normalize_visibility(data.get("visibility")),
            status=_claim_status_value(data.get("status")),
            attempt=_attempt_ref(attempt_payload) if attempt_payload else None,
            evidence_ids=data.get("evidence_ids") or (),
            validations=tuple(
                ValidationRecord.from_dict(item)
                for item in validations
                if isinstance(item, Mapping)
            ),
            binding=_binding_of(data.get("binding")),
            confidence=_confidence(data.get("confidence", 0.0)),
            human_locked=bool(data.get("human_locked", False)),
            superseded_by=_text(data.get("superseded_by")),
            retract_reason=safe_text(data.get("retract_reason"), limit=MAX_LIFECYCLE_REASON_CHARS),
            created_at=safe_text(data.get("created_at"), limit=40),
            updated_at=safe_text(data.get("updated_at"), limit=40),
            record_version=max(0, _int(data.get("record_version"))),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: ids/counts/flags only, never the statement."""

        return {
            "claim_id": self.claim_id,
            "scope": self.scope.key(),
            "visibility": self.visibility.value,
            "status": self.status.value,
            "confidence": self.confidence,
            "human_locked": self.human_locked,
            "attempt_key": self.attempt.key() if self.attempt is not None else "",
            "evidence_count": len(self.evidence_ids),
            "validation_count": len(self.validations),
            "record_version": self.record_version,
        }


# ---------------------------------------------------------------------------
# Stored projections and private control-flow exceptions
# ---------------------------------------------------------------------------


def _evidence_data(record: EvidenceRecord) -> dict:
    data = record.to_dict()
    # Flat index columns: RuntimeState._metadata reads ``task_id``.
    data["task_id"] = record.attempt.task_id
    data["attempt_key"] = record.attempt.key()
    return data


def _claim_data(record: ClaimRecord) -> dict:
    data = record.to_dict()
    data["task_id"] = record.attempt.task_id if record.attempt is not None else ""
    data["attempt_key"] = record.attempt.key() if record.attempt is not None else ""
    return data


class _ClaimNoOpError(Exception):
    """Abort a write transaction because the claim already has the wanted state."""

    def __init__(self, claim: ClaimRecord):
        self.claim = claim
        super().__init__("claim unchanged")


class _PromotionDeniedError(Exception):
    """Abort a promotion transaction because a fresh gate re-evaluation denied it."""

    def __init__(self, decision: GateDecision):
        self.decision = decision
        super().__init__("promotion denied")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class EvidenceLedger:
    """RuntimeState-backed evidence/claim/validation ledger.

    ``stage`` is resolved once at construction (explicit ``stage=`` wins over
    ``UC_KNOWLEDGE_STAGE``) because a worker must not widen grants mid-task.
    The ledger never mutates on denial: every denial path returns before
    ``RuntimeState.mutate`` or aborts the transaction from inside it.
    """

    def __init__(self, state: RuntimeState, *, stage: KnowledgeStage | None = None):
        self.state = state
        self.stage = resolve_stage(stage) if stage is not None else stage_from_env()
        self.gate = EpistemicGate()

    # -- evidence ----------------------------------------------------------

    def record_evidence(
        self,
        *,
        attempt: AttemptRef,
        kind: EvidenceKind,
        payload: Mapping | None = None,
        scope: KnowledgeScope | None = None,
        visibility: Visibility = Visibility.ATTEMPT,
        revision: RevisionBinding | None = None,
        summary: str = "",
        trust_class: str | None = None,
        verified: bool | None = None,
        evidence_id: str | None = None,
    ) -> EvidenceRecord:
        """Store one bounded observation, idempotently.

        ``evidence_id`` defaults to a deterministic digest of
        ``(kind, attempt key, revision, bounded payload)`` so retrying the same
        capture cannot duplicate evidence. Trust class and ``verified`` default
        from :mod:`model`; scope defaults to the attempt graph.
        """

        attempt_value = _attempt_ref(attempt)
        kind_value = normalize_evidence_kind(kind)
        visibility_value = normalize_visibility(visibility)
        revision_value = _binding_of(revision)
        if scope is not None:
            scope_value = _scope_of(scope)
        else:
            scope_value = KnowledgeScope(graph_id=attempt_value.graph_id)
        payload_value = _bounded_payload(payload)
        digest_id = _evidence_digest(kind_value, attempt_value, revision_value, payload_value)
        requested_id = safe_text(evidence_id, limit=MAX_ID_CHARS).strip()
        record = EvidenceRecord(
            evidence_id=requested_id or digest_id,
            kind=kind_value,
            attempt=attempt_value,
            scope=scope_value,
            visibility=visibility_value,
            revision=revision_value,
            payload=payload_value,
            summary=summary,
            trust_class=(
                default_trust_class(kind_value)
                if trust_class is None
                else safe_text(trust_class, limit=MAX_ID_CHARS)
            ),
            verified=default_verified(kind_value) if verified is None else bool(verified),
            created_at=utc_now_iso(),
            record_version=1,
        )
        existing = self.state.versioned_get(NAMESPACE, record.evidence_id)
        if existing is not None:
            return EvidenceRecord.from_dict(existing.data)
        data = _evidence_data(record)
        self.state.mutate(
            NAMESPACE,
            record.evidence_id,
            lambda current: current if current else data,
            audit={
                "action": "record_evidence",
                "evidence_id": record.evidence_id,
                "kind": record.kind.value,
                "attempt": record.attempt.key(),
                "visibility": record.visibility.value,
            },
        )
        return record

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        """Load one evidence record by id, or ``None``."""

        record = self.state.versioned_get(NAMESPACE, _text(evidence_id))
        if record is None:
            return None
        return EvidenceRecord.from_dict(record.data)

    def evidence_for(
        self,
        attempt: AttemptRef,
        *,
        kind: EvidenceKind | None = None,
        limit: int = 100,
    ) -> list:
        """Evidence captured by exactly this attempt, oldest first.

        Attempt identity is exact: a retry with a different ``dispatch_attempt``
        or ``attempt`` index does not inherit evidence implicitly (recovery code
        must ask for the chain explicitly).
        """

        attempt_value = _attempt_ref(attempt)
        kind_value = normalize_evidence_kind(kind) if kind is not None else None
        cap = _limit(limit, default=100, maximum=1_000)
        if cap <= 0:
            return []
        rows = self.state.query(
            NAMESPACE,
            task_id=attempt_value.task_id or None,
            limit=1_000,
        )
        records = []
        for row in rows:
            if row.get("attempt_key") != attempt_value.key():
                continue
            record = EvidenceRecord.from_dict(row)
            if kind_value is not None and record.kind is not kind_value:
                continue
            records.append(record)
        records.sort(key=lambda item: (item.created_at, item.evidence_id))
        return records[:cap]

    # -- claims ------------------------------------------------------------

    def record_claim(
        self,
        *,
        statement: str,
        scope: KnowledgeScope,
        attempt: AttemptRef | None = None,
        evidence_ids: Iterable[str] = (),
        binding: RevisionBinding | None = None,
        visibility: Visibility = Visibility.ATTEMPT,
        confidence: float = 0.0,
    ) -> ClaimRecord:
        """Record (or replay) a claim that is deterministic by scope+statement.

        Replaying the same statement in the same scope returns the existing
        claim untouched - including its status, validations and visibility. A
        new interpretation must be a different statement or a different scope.
        """

        scope_value = _scope_of(scope)
        statement_value = _normalize_statement(statement)
        claim_id = short_digest(
            {"scope": scope_value.to_dict(), "statement": statement_value.casefold()}
        )
        attempt_value = _attempt_ref(attempt) if attempt is not None else None
        existing = self.state.versioned_get(CLAIM_NAMESPACE, claim_id)
        if existing is not None:
            return ClaimRecord.from_dict(existing.data)
        now = utc_now_iso()
        record = ClaimRecord(
            claim_id=claim_id,
            statement=statement_value,
            scope=scope_value,
            visibility=normalize_visibility(visibility),
            status=ClaimStatus.CANDIDATE,
            attempt=attempt_value,
            evidence_ids=_dedupe_ids(evidence_ids),
            validations=(),
            binding=_binding_of(binding),
            confidence=_confidence(confidence),
            human_locked=False,
            created_at=now,
            updated_at=now,
            record_version=1,
        )
        data = _claim_data(record)
        self.state.mutate(
            CLAIM_NAMESPACE,
            claim_id,
            lambda current: current if current else data,
            audit={
                "action": "record_claim",
                "claim_id": claim_id,
                "visibility": record.visibility.value,
                "status": record.status.value,
            },
        )
        return record

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        """Load one claim by id, or ``None``."""

        record = self.state.versioned_get(CLAIM_NAMESPACE, _text(claim_id))
        if record is None:
            return None
        return ClaimRecord.from_dict(record.data)

    def validate_claim(
        self,
        claim_id: str,
        *,
        result: ValidationResult,
        revision: str = "",
        evidence_ids: Iterable[str] = (),
        strategy: str = "",
        validator: str = "",
        notes: str = "",
    ) -> tuple:
        """Append one validation outcome and return ``(claim, validation)``.

        A repeated validation of the same ``(claim, result, revision, evidence,
        strategy, validator)`` is idempotent (``notes`` are deliberately not
        part of the digest). The first PASS marks the claim VALIDATED; a later
        FAIL does not unset that history - the gate looks at the latest
        validation, so a FAIL still blocks promotion.
        """

        claim = self.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"unknown claim: {claim_id}")
        result_value = normalize_validation_result(result)
        revision_value = safe_text(revision, limit=MAX_ID_CHARS).strip()
        evidence_values = _dedupe_ids(evidence_ids)
        strategy_value = safe_text(strategy, limit=MAX_ID_CHARS).strip()
        validator_value = safe_text(validator, limit=MAX_ID_CHARS).strip()
        validation_id = _validation_digest(
            claim.claim_id,
            result_value,
            revision_value,
            evidence_values,
            strategy_value,
            validator_value,
        )
        existing = _find_validation(claim, validation_id)
        if existing is not None:
            return claim, existing
        validation = ValidationRecord(
            validation_id=validation_id,
            claim_id=claim.claim_id,
            result=result_value,
            revision=revision_value,
            evidence_ids=evidence_values,
            strategy=strategy_value,
            validator=validator_value,
            notes=safe_text(notes, limit=MAX_LIFECYCLE_REASON_CHARS),
            created_at=utc_now_iso(),
        )

        def apply(fresh: ClaimRecord) -> ClaimRecord:
            if _find_validation(fresh, validation_id) is not None:
                return fresh
            validations = (tuple(fresh.validations) + (validation,))[-MAX_VALIDATIONS:]
            status = fresh.status
            if result_value is ValidationResult.PASS and status not in (
                ClaimStatus.PROMOTED,
                ClaimStatus.SUPERSEDED,
                ClaimStatus.RETRACTED,
            ):
                status = ClaimStatus.VALIDATED
            return replace(
                fresh,
                validations=validations,
                status=status,
                updated_at=utc_now_iso(),
                record_version=fresh.record_version + 1,
            )

        updated = self._apply_claim_change(claim.claim_id, apply, action="validate_claim")
        stored = _find_validation(updated, validation_id)
        return updated, (stored if stored is not None else validation)

    # -- promotion and lifecycle ------------------------------------------

    def promote(
        self,
        claim_id: str,
        *,
        target: Visibility = Visibility.GRAPH,
        attempt_succeeded: bool = False,
        attempt_stale: bool = False,
        integration: RevisionBinding | None = None,
        push_status: str = "",
        target_revision: str = "",
        gate: EpistemicGate | None = None,
        human_locked: bool | None = None,
    ) -> tuple:
        """Promote a claim if the gate allows it; never mutates on denial.

        Returns ``(claim, decision)`` where ``claim`` is unchanged on denial.
        The gate runs again inside the compare-and-set transaction, so a claim
        that became terminal or gained a failing validation concurrently can
        never be promoted by a stale decision. ``integration=None`` falls back
        to the claim's own stored binding, and ``target_revision`` falls back to
        the binding revision. ``human_locked`` (when provided) is written in the
        same atomic mutation as the promotion.
        """

        claim = self.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"unknown claim: {claim_id}")
        gate_value = gate if gate is not None else self.gate
        integration_value = _binding_of(integration) if integration is not None else None
        lock_value = None if human_locked is None else bool(human_locked)

        decision = self._promotion_decision(
            claim,
            gate_value,
            target=target,
            attempt_succeeded=attempt_succeeded,
            attempt_stale=attempt_stale,
            integration=integration_value,
            push_status=push_status,
            target_revision=target_revision,
        )
        if not decision.allowed:
            return claim, decision

        def apply(fresh: ClaimRecord) -> ClaimRecord:
            fresh_decision = self._promotion_decision(
                fresh,
                gate_value,
                target=target,
                attempt_succeeded=attempt_succeeded,
                attempt_stale=attempt_stale,
                integration=integration_value,
                push_status=push_status,
                target_revision=target_revision,
            )
            if not fresh_decision.allowed:
                raise _PromotionDeniedError(fresh_decision)
            return replace(
                fresh,
                visibility=fresh_decision.visibility,
                status=ClaimStatus.PROMOTED,
                human_locked=fresh.human_locked if lock_value is None else lock_value,
                updated_at=utc_now_iso(),
                record_version=fresh.record_version + 1,
            )

        try:
            updated = self._apply_claim_change(claim.claim_id, apply, action="promote_claim")
        except _PromotionDeniedError as denied:
            current = self.get_claim(claim.claim_id)
            return (current if current is not None else claim), denied.decision
        return updated, decision

    def supersede(
        self,
        claim_id: str,
        *,
        new_claim_id: str = "",
        reason: str = "",
    ) -> ClaimRecord:
        """Mark a claim superseded by a newer one (terminal, never deleted).

        Repeating the call on a terminal claim is a no-op. A human-locked claim
        raises ``ValueError``: unlocking is an explicit human action.
        """

        claim = self.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"unknown claim: {claim_id}")
        if claim.is_terminal():
            return claim
        if claim.human_locked:
            raise ValueError("claim is human-locked; unlock before superseding")
        superseded_by = safe_text(new_claim_id, limit=MAX_ID_CHARS).strip()
        reason_value = safe_text(reason, limit=MAX_LIFECYCLE_REASON_CHARS)

        def apply(fresh: ClaimRecord) -> ClaimRecord:
            if fresh.is_terminal():
                return fresh
            if fresh.human_locked:
                raise ValueError("claim is human-locked; unlock before superseding")
            return replace(
                fresh,
                status=ClaimStatus.SUPERSEDED,
                superseded_by=superseded_by,
                retract_reason=reason_value,
                updated_at=utc_now_iso(),
                record_version=fresh.record_version + 1,
            )

        return self._apply_claim_change(claim.claim_id, apply, action="supersede_claim")

    def retract(self, claim_id: str, *, reason: str = "") -> ClaimRecord:
        """Retract a claim as an explicit human action (allowed even when locked).

        Retraction never deletes the record: the status and reason stay visible
        to the gate, which denies any later promotion via ``claim_terminal``.
        """

        claim = self.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"unknown claim: {claim_id}")
        if claim.is_terminal():
            return claim
        reason_value = safe_text(reason, limit=MAX_LIFECYCLE_REASON_CHARS)

        def apply(fresh: ClaimRecord) -> ClaimRecord:
            if fresh.is_terminal():
                return fresh
            return replace(
                fresh,
                status=ClaimStatus.RETRACTED,
                retract_reason=reason_value,
                updated_at=utc_now_iso(),
                record_version=fresh.record_version + 1,
            )

        return self._apply_claim_change(claim.claim_id, apply, action="retract_claim")

    def lock(self, claim_id: str, *, locked: bool = True) -> ClaimRecord:
        """Set or clear the human lock; idempotent when already in that state."""

        claim = self.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"unknown claim: {claim_id}")
        target = bool(locked)
        if claim.human_locked == target:
            return claim

        def apply(fresh: ClaimRecord) -> ClaimRecord:
            if fresh.human_locked == target:
                return fresh
            return replace(
                fresh,
                human_locked=target,
                updated_at=utc_now_iso(),
                record_version=fresh.record_version + 1,
            )

        return self._apply_claim_change(claim.claim_id, apply, action="lock_claim")

    # -- reads -------------------------------------------------------------

    def visible(
        self,
        *,
        scope: KnowledgeScope,
        reader_visibility: Visibility = Visibility.ATTEMPT,
        reader_scope: KnowledgeScope | None = None,
        reader_revision: str = "",
        same_chain: bool = False,
        min_visibility: Visibility = Visibility.ATTEMPT,
        limit: int = 50,
    ) -> list:
        """Validated/promoted claims the reader may see, best first.

        Applies, in order: non-terminal status, minimum visibility, scope
        isolation against the reader's scope, the gate read rules for the
        reader's visibility/scope/revision, and an extra revision-binding check.
        Sort is deterministic: visibility rank descending, confidence
        descending, claim id ascending.
        """

        scope_value = _scope_of(scope)
        reader_visibility_value = normalize_visibility(reader_visibility)
        reader_scope_value = scope_value if reader_scope is None else _scope_of(reader_scope)
        revision_value = safe_text(reader_revision, limit=MAX_ID_CHARS).strip()
        minimum_rank = visibility_rank(min_visibility)
        cap = _limit(limit, default=50, maximum=1_000)
        if cap <= 0:
            return []
        claims = []
        for row in self.state.records(CLAIM_NAMESPACE):
            claim = ClaimRecord.from_dict(row)
            if claim.status not in (ClaimStatus.VALIDATED, ClaimStatus.PROMOTED):
                continue
            if visibility_rank(claim.visibility) < minimum_rank:
                continue
            if not claim.scope.allows_read(reader_scope_value):
                continue
            decision = self.gate.can_read(
                record_visibility=claim.visibility,
                record_scope=claim.scope,
                reader_visibility=reader_visibility_value,
                reader_scope=reader_scope_value,
                binding=claim.binding,
                reader_revision=revision_value,
                same_chain=same_chain,
            )
            if not decision.allowed:
                continue
            if (
                claim.binding.revision.strip()
                and revision_value
                and not claim.binding.applies_to(revision_value)
            ):
                continue
            claims.append(claim)
        claims.sort(
            key=lambda item: (
                -visibility_rank(item.visibility),
                -float(item.confidence),
                item.claim_id,
            )
        )
        return claims[:cap]

    # -- internals ---------------------------------------------------------

    def _promotion_decision(
        self,
        claim: ClaimRecord,
        gate: EpistemicGate,
        *,
        target: Visibility,
        attempt_succeeded: bool,
        attempt_stale: bool,
        integration: RevisionBinding | None,
        push_status: str,
        target_revision: str,
    ) -> GateDecision:
        target_visibility = normalize_visibility(target)
        integration_value = integration
        if integration_value is None and claim.binding.has_integration:
            integration_value = claim.binding
        target_revision_value = (
            safe_text(target_revision, limit=MAX_ID_CHARS).strip()
            or claim.binding.revision.strip()
        )
        latest = claim.latest_validation()
        request = PromotionRequest(
            claim_visibility=claim.visibility,
            target_visibility=target_visibility,
            scope=claim.scope,
            status=claim.status,
            attempt_succeeded=attempt_succeeded,
            attempt_stale=attempt_stale,
            validation_result=latest.result if latest is not None else None,
            validation_revision=latest.revision if latest is not None else "",
            target_revision=target_revision_value,
            integration=integration_value,
            push_status=push_status,
            human_locked=claim.human_locked,
        )
        decision = gate.evaluate_promotion(request)
        reasons = list(decision.reasons)
        stage_disabled = (
            target_visibility is Visibility.GRAPH
            and self.stage < KnowledgeStage.GRAPH
        ) or (
            target_visibility is Visibility.REPOSITORY
            and self.stage < KnowledgeStage.CANONICAL
        )
        if stage_disabled:
            reasons.append(REASON_STAGE_DISABLED)
        allowed = not reasons
        return GateDecision(
            allowed=allowed,
            visibility=target_visibility if allowed else normalize_visibility(claim.visibility),
            reasons=tuple(reasons),
        )

    def _apply_claim_change(self, claim_id: str, apply, *, action: str) -> ClaimRecord:
        """Compare-and-set a claim change, retrying concurrent version bumps.

        ``apply(claim) -> ClaimRecord`` runs inside the write transaction and may
        abort by raising (the transaction rolls back). Retries at most
        :data:`CAS_RETRIES` times on :class:`RecordConflictError`.
        """

        last_conflict: RecordConflictError | None = None
        for _ in range(CAS_RETRIES + 1):
            current = self.get_claim(claim_id)
            if current is None:
                raise ValueError(f"unknown claim: {claim_id}")
            versioned = self.state.versioned_get(CLAIM_NAMESPACE, claim_id)
            if versioned is None:
                raise ValueError(f"unknown claim: {claim_id}")
            box = {}

            def change(data, expected=versioned.version):
                fresh = ClaimRecord.from_dict(data)
                updated = apply(fresh)
                if updated is None or updated == fresh:
                    raise _ClaimNoOpError(fresh)
                box["claim"] = updated
                return _claim_data(updated)

            try:
                self.state.mutate(
                    CLAIM_NAMESPACE,
                    claim_id,
                    change,
                    expected_version=versioned.version,
                    audit={"action": action, "claim_id": claim_id},
                )
                return box["claim"]
            except _ClaimNoOpError as noop:
                return noop.claim
            except RecordConflictError as conflict:
                last_conflict = conflict
        if last_conflict is not None:
            raise last_conflict
        raise RuntimeError("claim change retry loop exhausted without a conflict")
