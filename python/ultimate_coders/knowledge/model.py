"""Shared vocabulary for UltimateCoders' evidence-first knowledge layer.

Every knowledge record in this package answers the five questions the V2.0
upgrade asks of it: where it came from (evidence), whether it was verified
(validation), which code revision it applies to (revision binding), who may
read it (visibility + scope), and when it stops being current (lifecycle).

This module is intentionally import-light: pure types, enums and helpers used
by the worker, the ledgers and the (future) Rust knowledge-authority facade.
It must not import ``runtime_state`` or any agent module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "AttemptRef",
    "ClaimStatus",
    "DEFAULT_EVIDENCE_TRUST",
    "EvidenceKind",
    "KnowledgeScope",
    "RevisionBinding",
    "Visibility",
    "canonical_json",
    "digest",
    "default_trust_class",
    "default_verified",
    "normalize_claim_status",
    "normalize_evidence_kind",
    "normalize_validation_result",
    "normalize_visibility",
    "safe_text",
    "short_digest",
    "utc_now",
    "utc_now_iso",
    "validation_result_passed",
    "visibility_rank",
]


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Timezone-aware UTC now (never a naive datetime)."""

    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Second-precision ISO-8601 timestamp used for audit columns."""

    return utc_now().isoformat(timespec="seconds")


def canonical_json(payload: Any) -> str:
    """Deterministic JSON used for digests and idempotent record keys."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def digest(value: Any) -> str:
    """Stable SHA-256 hex digest of any JSON-serialisable value."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def short_digest(value: Any, length: int = 16) -> str:
    """Short digest, used for readable record ids and manifest summaries."""

    length = max(4, min(int(length), 64))
    return digest(value)[:length]


def safe_text(value: Any, *, limit: int = 2000) -> str:
    """Clip arbitrary text for log-safe / prompt-safe records.

    NUL bytes are removed (PostgreSQL rejects them), the value is coerced to
    ``str`` and clipped with an explicit marker so a reader can tell the text
    was truncated. Never use this on secrets; this helper only bounds size.
    """

    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Visibility: attempt-local -> graph-scoped -> repository-canonical
# ---------------------------------------------------------------------------


class Visibility(str, Enum):
    """How far a knowledge record may travel.

    ATTEMPT     - readable only by the producing attempt (retry recovery).
    GRAPH       - readable by every attempt in the same graph/task.
    REPOSITORY  - canonical for the repository revision it is bound to.
    """

    ATTEMPT = "attempt"
    GRAPH = "graph"
    REPOSITORY = "repository"


VISIBILITY_RANK = {
    Visibility.ATTEMPT: 0,
    Visibility.GRAPH: 1,
    Visibility.REPOSITORY: 2,
}

_VISIBILITY_ALIASES = {
    "": Visibility.ATTEMPT,
    "attempt": Visibility.ATTEMPT,
    "attempt-local": Visibility.ATTEMPT,
    "attempt_local": Visibility.ATTEMPT,
    "local": Visibility.ATTEMPT,
    "graph": Visibility.GRAPH,
    "graph-scoped": Visibility.GRAPH,
    "graph_scoped": Visibility.GRAPH,
    "task": Visibility.GRAPH,
    "repository": Visibility.REPOSITORY,
    "repo": Visibility.REPOSITORY,
    "canonical": Visibility.REPOSITORY,
    "long-term": Visibility.REPOSITORY,
    "long_term": Visibility.REPOSITORY,
}


def normalize_visibility(value: Any) -> Visibility:
    """Coerce a config/env/ledger value into :class:`Visibility`."""

    if isinstance(value, Visibility):
        return value
    key = str(value or "").strip().lower()
    try:
        return _VISIBILITY_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown knowledge visibility: {value!r}") from exc


def visibility_rank(value: Any) -> int:
    """Numeric rank of a visibility value (higher travels further)."""

    return VISIBILITY_RANK[normalize_visibility(value)]


# ---------------------------------------------------------------------------
# Evidence kinds and their default trust class
# ---------------------------------------------------------------------------


class EvidenceKind(str, Enum):
    """What an evidence record actually observes.

    Principle A of the plan: observations of facts and interpretations are
    separate kinds. ``PATCH_OBSERVED`` proves a diff happened;
    ``ORACLE_VERDICT`` proves a snapshot failed a check; neither alone proves
    causation.
    """

    PATCH_OBSERVED = "patch_observed"
    ATTEMPT_OUTCOME = "attempt_outcome"
    FAILURE_SIGNATURE = "failure_signature"
    ORACLE_VERDICT = "oracle_verdict"
    TEST_VERDICT = "test_verdict"
    REVIEW_VERDICT = "review_verdict"
    MERGE_RECEIPT = "merge_receipt"
    PUSH_VERDICT = "push_verdict"
    HYPOTHESIS = "hypothesis"
    VALIDATION = "validation"
    HUMAN_NOTE = "human_note"


_EVIDENCE_KIND_ALIASES = {
    "patch": EvidenceKind.PATCH_OBSERVED,
    "patch_observed": EvidenceKind.PATCH_OBSERVED,
    "diff": EvidenceKind.PATCH_OBSERVED,
    "patch-observed": EvidenceKind.PATCH_OBSERVED,
    "attempt_outcome": EvidenceKind.ATTEMPT_OUTCOME,
    "attempt-outcome": EvidenceKind.ATTEMPT_OUTCOME,
    "outcome": EvidenceKind.ATTEMPT_OUTCOME,
    "failure_signature": EvidenceKind.FAILURE_SIGNATURE,
    "failure-signature": EvidenceKind.FAILURE_SIGNATURE,
    "failure": EvidenceKind.FAILURE_SIGNATURE,
    "oracle_verdict": EvidenceKind.ORACLE_VERDICT,
    "oracle-verdict": EvidenceKind.ORACLE_VERDICT,
    "oracle": EvidenceKind.ORACLE_VERDICT,
    "test_verdict": EvidenceKind.TEST_VERDICT,
    "test-verdict": EvidenceKind.TEST_VERDICT,
    "test": EvidenceKind.TEST_VERDICT,
    "review_verdict": EvidenceKind.REVIEW_VERDICT,
    "review-verdict": EvidenceKind.REVIEW_VERDICT,
    "review": EvidenceKind.REVIEW_VERDICT,
    "merge_receipt": EvidenceKind.MERGE_RECEIPT,
    "merge-receipt": EvidenceKind.MERGE_RECEIPT,
    "merge": EvidenceKind.MERGE_RECEIPT,
    "push_verdict": EvidenceKind.PUSH_VERDICT,
    "push-verdict": EvidenceKind.PUSH_VERDICT,
    "push": EvidenceKind.PUSH_VERDICT,
    "hypothesis": EvidenceKind.HYPOTHESIS,
    "validation": EvidenceKind.VALIDATION,
    "human_note": EvidenceKind.HUMAN_NOTE,
    "human-note": EvidenceKind.HUMAN_NOTE,
    "human": EvidenceKind.HUMAN_NOTE,
}


def normalize_evidence_kind(value: Any) -> EvidenceKind:
    """Coerce an enum/string value into :class:`EvidenceKind`."""

    if isinstance(value, EvidenceKind):
        return value
    key = str(value or "").strip().lower()
    try:
        return _EVIDENCE_KIND_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown evidence kind: {value!r}") from exc


DEFAULT_EVIDENCE_TRUST = {
    EvidenceKind.PATCH_OBSERVED: "git_diff",
    EvidenceKind.ATTEMPT_OUTCOME: "worker_reported",
    EvidenceKind.FAILURE_SIGNATURE: "worker_reported",
    EvidenceKind.ORACLE_VERDICT: "oracle",
    EvidenceKind.TEST_VERDICT: "test_runner",
    EvidenceKind.REVIEW_VERDICT: "reviewer",
    EvidenceKind.MERGE_RECEIPT: "git_target",
    EvidenceKind.PUSH_VERDICT: "git_remote",
    EvidenceKind.HYPOTHESIS: "hy_memory_candidate",
    EvidenceKind.VALIDATION: "validator",
    EvidenceKind.HUMAN_NOTE: "human",
}

#: Kinds whose payload is machine-observed rather than model-reported.
VERIFIED_EVIDENCE_KINDS = frozenset(
    {
        EvidenceKind.PATCH_OBSERVED,
        EvidenceKind.ORACLE_VERDICT,
        EvidenceKind.TEST_VERDICT,
        EvidenceKind.REVIEW_VERDICT,
        EvidenceKind.MERGE_RECEIPT,
        EvidenceKind.PUSH_VERDICT,
        EvidenceKind.HUMAN_NOTE,
    }
)


def default_trust_class(kind: Any) -> str:
    """Trust class a ledger stamps on evidence of ``kind``."""

    return DEFAULT_EVIDENCE_TRUST[normalize_evidence_kind(kind)]


def default_verified(kind: Any) -> bool:
    """Whether evidence of ``kind`` is machine-observed by default."""

    return normalize_evidence_kind(kind) in VERIFIED_EVIDENCE_KINDS


# ---------------------------------------------------------------------------
# Claim lifecycle and validation outcomes
# ---------------------------------------------------------------------------


class ClaimStatus(str, Enum):
    """Lifecycle of a long-term knowledge claim."""

    CANDIDATE = "candidate"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


_CLAIM_STATUS_ALIASES = {status.value: status for status in ClaimStatus}


def normalize_claim_status(value: Any) -> ClaimStatus:
    """Coerce an enum/string value into :class:`ClaimStatus`."""

    if isinstance(value, ClaimStatus):
        return value
    key = str(value or "").strip().lower()
    try:
        return _CLAIM_STATUS_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown claim status: {value!r}") from exc


class ValidationResult(str, Enum):
    """Outcome of the check that validates a claim (or a fix attempt)."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


_VALIDATION_ALIASES = {
    "pass": ValidationResult.PASS,
    "passed": ValidationResult.PASS,
    "ok": ValidationResult.PASS,
    "success": ValidationResult.PASS,
    "succeeded": ValidationResult.PASS,
    "fail": ValidationResult.FAIL,
    "failed": ValidationResult.FAIL,
    "failure": ValidationResult.FAIL,
    "error": ValidationResult.FAIL,
    "inconclusive": ValidationResult.INCONCLUSIVE,
    "unknown": ValidationResult.INCONCLUSIVE,
    "pending": ValidationResult.INCONCLUSIVE,
    "": ValidationResult.INCONCLUSIVE,
}


def normalize_validation_result(value: Any) -> ValidationResult:
    """Coerce an enum/string/bool value into :class:`ValidationResult`."""

    if isinstance(value, ValidationResult):
        return value
    if isinstance(value, bool):
        return ValidationResult.PASS if value else ValidationResult.FAIL
    key = str(value or "").strip().lower()
    try:
        return _VALIDATION_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown validation result: {value!r}") from exc


def validation_result_passed(value: Any) -> bool:
    """True only for an explicit PASS (INCONCLUSIVE never counts)."""

    return normalize_validation_result(value) is ValidationResult.PASS


# ---------------------------------------------------------------------------
# Attempt identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRef:
    """Identity of one logical attempt inside a task graph.

    ``dispatch_attempt`` is the worker dispatch counter
    (``subtask.dispatch_retry_count``); ``attempt`` is the in-worker retry
    index. Together with task/subtask ids they form the idempotency key for
    patch rounds, evidence and decision records.
    """

    task_id: str
    subtask_id: str
    dispatch_attempt: int = 0
    attempt: int = 0
    worker_id: str = ""
    worker_epoch: int = 0
    graph_id: str = ""

    def key(self) -> str:
        return (
            f"{self.task_id}:{self.subtask_id}:"
            f"{int(self.dispatch_attempt)}:{int(self.attempt)}"
        )

    def order(self) -> tuple:
        """Sortable (dispatch, attempt) position; earlier attempts sort first."""

        return (int(self.dispatch_attempt), int(self.attempt))

    def is_before(self, other: AttemptRef) -> bool:
        """True when this attempt logically precedes ``other``."""

        if self.task_id != other.task_id or self.subtask_id != other.subtask_id:
            return self.order() < other.order()
        return self.order() < other.order()

    def next_attempt(self) -> AttemptRef:
        """The in-worker retry that follows this one."""

        return replace(self, attempt=int(self.attempt) + 1)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "dispatch_attempt": int(self.dispatch_attempt),
            "attempt": int(self.attempt),
            "worker_id": self.worker_id,
            "worker_epoch": int(self.worker_epoch),
            "graph_id": self.graph_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AttemptRef:
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            task_id=str(data.get("task_id") or ""),
            subtask_id=str(data.get("subtask_id") or ""),
            dispatch_attempt=_as_int(data.get("dispatch_attempt")),
            attempt=_as_int(data.get("attempt")),
            worker_id=str(data.get("worker_id") or ""),
            worker_epoch=_as_int(data.get("worker_epoch")),
            graph_id=str(data.get("graph_id") or ""),
        )


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Scope and revision binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeScope:
    """Tenant/repository/branch/graph namespace of a knowledge record.

    Empty ``repo_id``/``branch`` fields are recorded only by callers that do
    not know them; on reads an empty record field is a wildcard while an empty
    *reader* field never widens access to a record that pins a value.
    """

    tenant_id: str = "default"
    repo_id: str = ""
    branch: str = ""
    graph_id: str = ""

    def key(self) -> str:
        """Stable string key used in audit/log-safe summaries."""

        return f"{self.tenant_id}:{self.repo_id}:{self.branch}"

    def allows_read(self, reader: KnowledgeScope, *, require_graph: bool = False) -> bool:
        """Whether ``reader`` may read a record in ``self``'s scope."""

        if self.tenant_id != reader.tenant_id:
            return False
        if self.repo_id and self.repo_id != reader.repo_id:
            return False
        if self.branch and self.branch != reader.branch:
            return False
        if require_graph:
            if not self.graph_id or not reader.graph_id:
                return False
            if self.graph_id != reader.graph_id:
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "repo_id": self.repo_id,
            "branch": self.branch,
            "graph_id": self.graph_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> KnowledgeScope:
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            tenant_id=str(data.get("tenant_id") or "default"),
            repo_id=str(data.get("repo_id") or ""),
            branch=str(data.get("branch") or ""),
            graph_id=str(data.get("graph_id") or ""),
        )


def _sha_matches(pinned: str, candidate: str) -> bool:
    """Exact or unambiguous short-SHA prefix match."""

    if pinned == candidate:
        return True
    if len(pinned) < 7 or len(candidate) < 7:
        return False
    return pinned.startswith(candidate) or candidate.startswith(pinned)


@dataclass(frozen=True)
class RevisionBinding:
    """Which code revision a fact applies to, and where it landed.

    ``revision``/``base_revision`` bind a patch or claim to the commit (and
    merge base) it was observed on. ``integration_ref``/``integration_sha``
    are filled only when the change is known to have landed on a target ref;
    without them a claim can never become repository-canonical.
    """

    repo_id: str = ""
    branch: str = ""
    revision: str = ""
    base_revision: str = ""
    integration_ref: str = ""
    integration_sha: str = ""

    @property
    def has_integration(self) -> bool:
        """True when a target ref + landed SHA are both recorded."""

        return bool(self.integration_ref.strip() and self.integration_sha.strip())

    def applies_to(self, revision: str) -> bool:
        """Whether the binding is valid for ``revision``.

        An unpinned binding (empty ``revision``) is a wildcard. A pinned
        binding matches the same commit or an unambiguous short-SHA prefix.
        """

        pinned = self.revision.strip()
        if not pinned:
            return True
        candidate = str(revision or "").strip()
        if not candidate:
            return False
        return _sha_matches(pinned, candidate)

    def to_dict(self) -> dict:
        return {
            "repo_id": self.repo_id,
            "branch": self.branch,
            "revision": self.revision,
            "base_revision": self.base_revision,
            "integration_ref": self.integration_ref,
            "integration_sha": self.integration_sha,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RevisionBinding:
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            repo_id=str(data.get("repo_id") or ""),
            branch=str(data.get("branch") or ""),
            revision=str(data.get("revision") or ""),
            base_revision=str(data.get("base_revision") or ""),
            integration_ref=str(data.get("integration_ref") or ""),
            integration_sha=str(data.get("integration_sha") or ""),
        )
