"""Integration receipts written by the merge arbiter.

The worker can only observe its own worktree; whether a subtask branch actually
landed on the target ref is seen by the ``MergeArbiter`` after it fast-forwards
or merges and pushes ``main``. These helpers turn that observation into the
authoritative facts the knowledge layer was missing:

* ``MERGE_RECEIPT`` - the target accepted the merge (commit sha, base branch);
* ``PUSH_VERDICT`` - what the remote said about the push (may be ``UNKNOWN``);

and, only at stage ``canonical`` and only for a graph-scoped integration, a
repository claim validated by the receipt and promoted through the gate.

Everything here is best-effort: arbitration must never fail because knowledge
capture failed, and knowledge capture must never be able to promote anything
the gate would deny.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ultimate_coders.knowledge import rollout
from ultimate_coders.knowledge.claims import record_gate_decision
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.model import (
    AttemptRef,
    EvidenceKind,
    KnowledgeScope,
    RevisionBinding,
    Visibility,
    safe_text,
)
from ultimate_coders.knowledge.pages import KnowledgePages

INTEGRATION_WORKER = "merge-arbiter"
INTEGRATION_STRATEGY = "integration-receipt"
MAX_BRANCHES = 50
MAX_BRANCH_ID_CHARS = 120
ACCEPTED_STATUS = "merged"
UNKNOWN_PUSH_STATUS = "UNKNOWN"


def integration_attempt(branch: str) -> AttemptRef:
    """Identity for one landed subtask branch.

    The arbiter has no task/graph id for a branch it merges, so the branch's
    own subtask id (last path segment) is the durable identity. Integration
    facts are target-scoped evidence, not attempt-chain facts.
    """

    subtask_id = safe_text(branch.rsplit("/", 1)[-1], limit=MAX_BRANCH_ID_CHARS).strip()
    return AttemptRef(
        task_id="",
        subtask_id=subtask_id or "unknown-branch",
        dispatch_attempt=0,
        attempt=0,
        worker_id=INTEGRATION_WORKER,
        graph_id="",
    )


def integration_binding(
    *,
    base_branch: str = "main",
    remote_name: str = "origin",
    integration_sha: str = "",
) -> RevisionBinding:
    """Binding saying where the change landed (target ref + landed sha)."""

    branch = safe_text(base_branch, limit=120).strip() or "main"
    remote = safe_text(remote_name, limit=80).strip()
    ref = f"{remote}/{branch}" if remote else branch
    sha = safe_text(integration_sha, limit=64).strip()
    return RevisionBinding(
        branch=branch,
        revision=sha,
        integration_ref=ref,
        integration_sha=sha,
    )


def record_integration(
    state: Any,
    *,
    branches: Iterable[str],
    base_branch: str = "main",
    remote_name: str = "origin",
    integration_sha: str = "",
    push_status: str = "",
    merged: bool = True,
    scope: KnowledgeScope | None = None,
    stage: rollout.KnowledgeStage | None = None,
) -> dict[str, Any]:
    """Record integration evidence (and optionally a repository claim).

    Returns a log-safe summary: branch/evidence counts, promotions and denial
    reasons. Never raises for bad data - the arbiter treats this as audit.
    """

    stage_value = stage if stage is not None else rollout.stage_from_env()
    branches_value = [str(branch) for branch in branches if str(branch or "").strip()]
    if len(branches_value) > MAX_BRANCHES:
        branches_value = branches_value[:MAX_BRANCHES]

    binding = integration_binding(
        base_branch=base_branch, remote_name=remote_name, integration_sha=integration_sha
    )
    claim_scope = scope or KnowledgeScope(branch=binding.branch)
    push_word = safe_text(push_status, limit=40).strip() or UNKNOWN_PUSH_STATUS

    if not rollout.capture_enabled(stage_value):
        # Stage A must be a pure pass-through: no writes at all.
        return {
            "stage": rollout.describe_stage(stage_value)["stage"],
            "branches": len(branches_value),
            "evidence_records": 0,
            "claims": 0,
            "promoted": 0,
            "denied": 0,
            "errors": 0,
            "pages": 0,
            "skipped": True,
        }

    ledger = EvidenceLedger(state, stage=stage_value)

    evidence_records = 0
    claims = 0
    promoted = 0
    denied = 0
    reasons: set[str] = set()
    errors = 0
    promoted_claims: list[Any] = []

    for branch in branches_value:
        try:
            attempt = integration_attempt(branch)
            payload = {
                "status": ACCEPTED_STATUS if merged else "not-merged",
                "branch": safe_text(branch, limit=MAX_BRANCH_ID_CHARS).strip(),
                "base_branch": binding.branch,
                "remote": safe_text(remote_name, limit=80).strip(),
                "commit_sha": binding.integration_sha,
            }
            # Target-level facts: the receipt says the *repository* accepted
            # the change, not that one graph's attempt is still valid.
            receipt = ledger.record_evidence(
                attempt=attempt,
                kind=EvidenceKind.MERGE_RECEIPT,
                payload=payload,
                scope=claim_scope,
                visibility=Visibility.REPOSITORY,
                revision=binding,
                summary="merge arbiter receipt",
            )
            push = ledger.record_evidence(
                attempt=attempt,
                kind=EvidenceKind.PUSH_VERDICT,
                payload={"status": binding.branch, "push_status": push_word},
                scope=claim_scope,
                visibility=Visibility.REPOSITORY,
                revision=binding,
                summary="merge arbiter push verdict",
            )
            evidence_records += 2
            if not rollout.canonical_enabled(stage_value) or not merged:
                continue
            if not claim_scope.graph_id.strip():
                # The gate requires a graph scope before anything can become
                # canonical; without one, receipts only (documented gap).
                continue
            claim = ledger.record_claim(
                statement=(
                    f"branch {payload['branch'] or attempt.subtask_id} landed on "
                    f"{binding.integration_ref or binding.branch} at revision "
                    f"{binding.integration_sha[:12] or 'unknown'}"
                ),
                scope=claim_scope,
                attempt=attempt,
                evidence_ids=(receipt.evidence_id, push.evidence_id),
                binding=binding,
                visibility=Visibility.ATTEMPT,
            )
            claims += 1
            ledger.validate_claim(
                claim.claim_id,
                result="pass",
                revision=binding.integration_sha,
                evidence_ids=(receipt.evidence_id, push.evidence_id),
                strategy=INTEGRATION_STRATEGY,
                validator=INTEGRATION_WORKER,
            )
            _claim, decision = ledger.promote(
                claim.claim_id,
                target=Visibility.REPOSITORY,
                attempt_succeeded=True,
                integration=binding,
                push_status=push_word,
                target_revision=binding.integration_sha,
            )
            record_gate_decision(
                ledger, claim=_claim, decision=decision, source="integration"
            )
            if decision.allowed:
                promoted += 1
                promoted_claims.append(_claim)
            else:
                denied += 1
                reasons.update(decision.reasons)
        except Exception:
            errors += 1

    pages = _rebuild_pages(state, claim_scope, promoted_claims, binding)
    if pages is None:
        errors += 1
    summary: dict[str, Any] = {
        "branches": len(branches_value),
        "evidence_records": evidence_records,
        "claims": claims,
        "promoted": promoted,
        "denied": denied,
        "errors": errors,
        "pages": pages or 0,
        "skipped": False,
    }
    if reasons:
        summary["reasons"] = sorted(reasons)
    return summary


def _rebuild_pages(
    state: Any,
    claim_scope: KnowledgeScope,
    promoted_claims: list[Any],
    binding: RevisionBinding,
) -> int | None:
    """Refresh the repository page derived from the promoted claims.

    Pages are a projection, so this runs *after* the authoritative claims were
    promoted (canonical stage only - ``promoted_claims`` is only populated
    there). ``1`` means one page was (re)derived, ``0`` means there was nothing
    to derive (no promoted claims in this batch), and ``None`` means the
    projection failed - the caller counts it as a knowledge-side error without
    ever failing arbitration.

    The page scope deliberately drops ``graph_id``: a canonical page describes
    the repository/branch, not one graph. The binding is passed explicitly so
    the page pins the landed revision and integration ref from the receipt,
    rather than guessing from a deepening head it does not have.
    """

    if not promoted_claims:
        return 0
    page_scope = KnowledgeScope(
        tenant_id=claim_scope.tenant_id,
        repo_id=claim_scope.repo_id,
        branch=claim_scope.branch,
    )
    page_binding = RevisionBinding(
        repo_id=page_scope.repo_id,
        branch=binding.branch,
        revision=binding.revision,
        base_revision=binding.base_revision,
        integration_ref=binding.integration_ref,
        integration_sha=binding.integration_sha,
    )
    try:
        KnowledgePages(state).rebuild(
            scope=page_scope, claims=promoted_claims, binding=page_binding
        )
        return 1
    except Exception:  # noqa: BLE001 - projections must never fail arbitration
        return None
