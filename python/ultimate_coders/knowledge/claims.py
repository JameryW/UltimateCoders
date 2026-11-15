"""Derive attempt-scoped knowledge claims and promote them only through the gate.

A claim is an interpretation built from evidence - "this attempt delivered and
merged these files at this revision" or "this attempt failed with this
signature". Claims are recorded at ATTEMPT visibility; the gate, not the
producer, decides whether they may leave the retry chain.

Promotion is a separate, explicit call: the worker validates a delivered claim
with the merge receipt as acceptance evidence and asks the ``EpistemicGate``
for GRAPH visibility. A denied promotion leaves the claim exactly as it was -
the caller never writes the target visibility itself (principle D: the ledger
is authoritative, projections rebuild from it).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ultimate_coders.knowledge.evidence import ClaimRecord, EvidenceLedger
from ultimate_coders.knowledge.gate import GateDecision
from ultimate_coders.knowledge.model import (
    AttemptRef,
    EvidenceKind,
    KnowledgeScope,
    RevisionBinding,
    ValidationResult,
    Visibility,
    safe_text,
)

CLAIM_VALIDATOR = "worker"
DELIVERY_STRATEGY = "merge-receipt"
FAILURE_STRATEGY = "failure-signature"
MAX_SIGNATURE_CHARS = 200
MAX_FILES = 20
ACCEPTED_MERGE_STATUSES = frozenset({"merged", "already_merged", "no_changes"})


def attempt_scope(attempt: AttemptRef, *, branch: str = "") -> KnowledgeScope:
    """The graph scope an attempt's claims belong to.

    ``graph_id`` comes from the attempt identity (the worker sets it to the
    parent task id), so a claim can only ever be considered for graph-wide
    sharing inside its own graph. ``branch`` is optional - a claim recorded
    before delivery has no target branch yet.
    """

    return KnowledgeScope(graph_id=attempt.graph_id or "", branch=branch)


def failure_claim_statement(attempt: AttemptRef, signature: str = "") -> str:
    """Deterministic statement for a failed attempt (negative experience)."""

    detail = safe_text(signature, limit=MAX_SIGNATURE_CHARS).strip()
    if not detail:
        detail = "no captured failure signature"
    return (
        f"attempt {int(attempt.attempt)} of subtask {attempt.subtask_id} "
        f"failed: {detail}"
    )


def delivery_claim_statement(
    attempt: AttemptRef,
    *,
    merge_status: str = "",
    file_count: int = 0,
    head_revision: str = "",
    verification_label: str = "",
) -> str:
    """Deterministic statement for an attempt that delivered and merged."""

    count = max(0, int(file_count))
    status = safe_text(merge_status, limit=40).strip() or "unknown-merge-status"
    revision = safe_text(head_revision, limit=12).strip() or "unknown-revision"
    statement = (
        f"attempt {int(attempt.attempt)} of subtask {attempt.subtask_id} delivered "
        f"{count} file(s) and reported {status} at revision {revision}"
    )
    label = safe_text(verification_label, limit=80).strip()
    if label and label != "unknown":
        statement += f" (verification: {label})"
    return statement


def record_failure_claim(
    ledger: EvidenceLedger,
    *,
    attempt: AttemptRef,
    scope: KnowledgeScope,
    evidence_ids: Iterable[str] = (),
    signature: str = "",
) -> ClaimRecord:
    """Record the attempt's failure claim at ATTEMPT visibility.

    Never promoted by the worker: a failure is negative knowledge for the next
    retry, and only the gate may ever widen its audience.
    """

    return ledger.record_claim(
        statement=failure_claim_statement(attempt, signature),
        scope=scope,
        attempt=attempt,
        evidence_ids=evidence_ids,
        binding=RevisionBinding(),
        visibility=Visibility.ATTEMPT,
    )


def record_delivery_claim(
    ledger: EvidenceLedger,
    *,
    attempt: AttemptRef,
    scope: KnowledgeScope,
    revision: str = "",
    evidence_ids: Iterable[str] = (),
    merge_status: str = "",
    file_count: int = 0,
    verification_label: str = "",
) -> ClaimRecord:
    """Record the delivered-and-merged claim at ATTEMPT visibility.

    The claim is an interpretation; the merge receipt evidence it links is the
    acceptance fact. Promotion is a separate call so a caller can record the
    claim even when no graph sharing is enabled yet.
    """

    binding = RevisionBinding(branch=scope.branch or "", revision=revision or "")
    return ledger.record_claim(
        statement=delivery_claim_statement(
            attempt,
            merge_status=merge_status,
            file_count=file_count,
            head_revision=revision,
            verification_label=verification_label,
        ),
        scope=scope,
        attempt=attempt,
        evidence_ids=evidence_ids,
        binding=binding,
        visibility=Visibility.ATTEMPT,
    )


GATE_DECISION_STRATEGY = "epistemic-gate"
MAX_DECISION_REASONS = 12


def record_gate_decision(
    ledger: EvidenceLedger,
    *,
    claim: ClaimRecord,
    decision: GateDecision,
    target: Visibility = Visibility.GRAPH,
    source: str = "delivery",
) -> str:
    """Record one gate decision as VALIDATION evidence (log-safe, idempotent).

    Denials were previously invisible: ``promote()`` returned reasons to the
    caller and nothing kept them, so the dashboard could not show *why*
    knowledge was refused. This writes the decision - allowed flag, bounded
    reason codes, target visibility, claim id - and never the claim statement.
    The evidence id is derived from that payload, so replaying the same
    decision does not inflate the counts.
    """

    attempt = claim.attempt or AttemptRef()
    bounded_source = safe_text(source, limit=40).strip()
    payload: dict[str, Any] = {
        "allowed": bool(decision.allowed),
        "reasons": [safe_text(reason, limit=80).strip() for reason in decision.reasons][
            :MAX_DECISION_REASONS
        ],
        "target": (decision.visibility or target).value,
        "claim_id": claim.claim_id,
        "strategy": GATE_DECISION_STRATEGY,
    }
    if bounded_source:
        payload["source"] = bounded_source
    record = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.VALIDATION,
        payload=payload,
        scope=claim.scope,
        visibility=Visibility.ATTEMPT,
        revision=claim.binding,
        summary="gate decision recorded",
    )
    return record.evidence_id


def validate_and_promote_delivery(
    ledger: EvidenceLedger,
    claim_id: str,
    *,
    revision: str = "",
    evidence_ids: Iterable[str] = (),
    push_status: str = "",
    target: Visibility = Visibility.GRAPH,
    attempt_succeeded: bool = True,
    attempt_stale: bool = False,
    integration: RevisionBinding | None = None,
) -> tuple[ClaimRecord, GateDecision]:
    """Validate a delivered claim with its receipt, then ask the gate to share it.

    The validation is a PASS because the merge receipt is the acceptance
    evidence (release is not validation on its own - the claim statement says
    what was delivered, and the receipt says the target accepted it). The gate
    still runs its own checks; a denial returns the claim unchanged.

    ``attempt_succeeded`` is passed through, never assumed: a delivery claim
    produced by a failed attempt must be denied even when the release path
    reported a merge.
    """

    evidence_values = tuple(evidence_ids)
    ledger.validate_claim(
        claim_id,
        result=ValidationResult.PASS,
        revision=revision or "",
        evidence_ids=evidence_values,
        strategy=DELIVERY_STRATEGY,
        validator=CLAIM_VALIDATOR,
    )
    claim, decision = ledger.promote(
        claim_id,
        target=target,
        attempt_succeeded=attempt_succeeded,
        attempt_stale=attempt_stale,
        integration=integration,
        push_status=push_status,
        target_revision=revision or "",
    )
    record_gate_decision(ledger, claim=claim, decision=decision, target=target)
    return claim, decision


def promote_failure_claim(
    ledger: EvidenceLedger,
    claim_id: str,
    *,
    target: Visibility = Visibility.GRAPH,
) -> tuple[ClaimRecord, GateDecision]:
    """Ask the gate about a failure claim without pretending it is verified.

    Deliberately never validates: a failure claim has no acceptance evidence,
    so the gate must deny graph sharing (``validation_missing``). Kept as an
    explicit call so the caller can record the denial instead of assuming it.
    """

    claim, decision = ledger.promote(claim_id, target=target, attempt_succeeded=False)
    record_gate_decision(ledger, claim=claim, decision=decision, target=target, source="failure")
    return claim, decision


def claim_summary(
    claim: ClaimRecord, decision: GateDecision | None = None
) -> dict[str, Any]:
    """Log-safe claim summary for events and receipts (no statement text)."""

    payload: dict[str, Any] = {
        "claim_id": claim.claim_id,
        "status": claim.status.value,
        "visibility": claim.visibility.value,
    }
    if decision is not None:
        payload["allowed"] = bool(decision.allowed)
        payload["decision_visibility"] = decision.visibility.value
        payload["reasons"] = list(decision.reasons)
    return payload
