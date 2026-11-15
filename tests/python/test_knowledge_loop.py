"""End-to-end evidence -> claim -> validation -> promotion loop.

This is the acceptance test for the V2.0 MVP's safety core: attempt-scoped
evidence can only become shared knowledge after validation, a successful
attempt, a revision match and (for repository scope) an integration receipt
whose push verdict is known. The gate must deny without mutating anything.
"""

from __future__ import annotations

import pytest
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.gate import (
    REASON_INTEGRATION_MISSING,
    REASON_PUSH_UNKNOWN,
    REASON_VALIDATION_FAILED,
    REASON_VALIDATION_MISSING,
)
from ultimate_coders.knowledge.model import (
    AttemptRef,
    ClaimStatus,
    EvidenceKind,
    KnowledgeScope,
    RevisionBinding,
    ValidationResult,
    Visibility,
)
from ultimate_coders.knowledge.rollout import KnowledgeStage
from ultimate_coders.runtime_state import RuntimeState

REVISION = "a" * 40
BASE_REVISION = "b" * 40


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("UC_KNOWLEDGE_STAGE", raising=False)


@pytest.fixture()
def state(tmp_path):
    return RuntimeState(tmp_path / "loop-state.sqlite3", database_url="")


def _scope() -> KnowledgeScope:
    return KnowledgeScope(tenant_id="tenant-a", repo_id="repo-1", branch="main", graph_id="g1")


def _attempt() -> AttemptRef:
    return AttemptRef(
        task_id="task-1",
        subtask_id="sub-1",
        dispatch_attempt=0,
        attempt=0,
        worker_id="worker-a",
        worker_epoch=1,
        graph_id="g1",
    )


def _binding() -> RevisionBinding:
    return RevisionBinding(
        repo_id="repo-1",
        branch="main",
        revision=REVISION,
        base_revision=BASE_REVISION,
    )


def _seeded_claim(ledger: EvidenceLedger):
    """Record the same facts the worker capture path records, then a claim."""
    attempt = _attempt()
    outcome = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.ATTEMPT_OUTCOME,
        payload={"success": True, "retryable": False, "attempt": 0},
        summary="fix applied",
    )
    receipt = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.MERGE_RECEIPT,
        payload={"status": "merged", "commit_sha": "c" * 40, "branch_preserved": ""},
        summary="workspace release",
    )
    push = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.PUSH_VERDICT,
        payload={"push_status": "pushed"},
        summary="branch push",
    )
    claim = ledger.record_claim(
        statement="the retry fix restores the failing auth test",
        scope=_scope(),
        attempt=attempt,
        evidence_ids=(outcome.evidence_id, receipt.evidence_id, push.evidence_id),
        binding=_binding(),
        visibility=Visibility.ATTEMPT,
    )
    return claim, receipt


def test_unvalidated_claim_can_never_be_promoted(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.CANONICAL)
    claim, _receipt = _seeded_claim(ledger)

    promoted, decision = ledger.promote(
        claim.claim_id, target=Visibility.GRAPH, attempt_succeeded=True
    )

    assert decision.denied is True
    assert REASON_VALIDATION_MISSING in decision.reasons
    # Denial never mutates the claim.
    after = ledger.get_claim(claim.claim_id)
    assert promoted.visibility is Visibility.ATTEMPT
    assert after is not None and after.visibility is Visibility.ATTEMPT
    assert after.status is ClaimStatus.CANDIDATE
    assert after.record_version == claim.record_version


def test_graph_promotion_requires_pass_and_successful_attempt(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    claim, receipt = _seeded_claim(ledger)

    ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.PASS,
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        strategy="pytest",
        validator="test-suite",
    )
    validated = ledger.get_claim(claim.claim_id)
    assert validated is not None and validated.status is ClaimStatus.VALIDATED

    # A failed attempt cannot promote a validated claim.
    _blocked, denied = ledger.promote(
        claim.claim_id, target=Visibility.GRAPH, attempt_succeeded=False
    )
    assert denied.denied is True

    promoted, decision = ledger.promote(
        claim.claim_id, target=Visibility.GRAPH, attempt_succeeded=True
    )
    assert decision.allowed is True
    assert promoted.visibility is Visibility.GRAPH
    assert promoted.status is ClaimStatus.PROMOTED


def test_failing_validation_blocks_promotion(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    claim, receipt = _seeded_claim(ledger)

    ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.PASS,
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        validator="test-suite",
    )
    ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.FAIL,
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        validator="review-suite",
    )

    _promoted, decision = ledger.promote(
        claim.claim_id, target=Visibility.GRAPH, attempt_succeeded=True
    )
    assert decision.denied is True
    assert REASON_VALIDATION_FAILED in decision.reasons


def test_repository_promotion_requires_integration_receipt(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.CANONICAL)
    claim, receipt = _seeded_claim(ledger)
    ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.PASS,
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        validator="test-suite",
    )

    # Validated + successful is still not enough for repository scope.
    _promoted, denied = ledger.promote(
        claim.claim_id,
        target=Visibility.REPOSITORY,
        attempt_succeeded=True,
        target_revision=REVISION,
    )
    assert denied.denied is True
    assert REASON_INTEGRATION_MISSING in denied.reasons

    # An unknown push verdict must be treated as unproven.
    _promoted, blocked_push = ledger.promote(
        claim.claim_id,
        target=Visibility.REPOSITORY,
        attempt_succeeded=True,
        target_revision=REVISION,
        push_status="UNKNOWN",
        integration=_binding_with_integration(),
    )
    assert blocked_push.denied is True
    assert REASON_PUSH_UNKNOWN in blocked_push.reasons

    promoted, allowed = ledger.promote(
        claim.claim_id,
        target=Visibility.REPOSITORY,
        attempt_succeeded=True,
        target_revision=REVISION,
        push_status="pushed",
        integration=_binding_with_integration(),
    )
    assert allowed.allowed is True
    assert promoted.visibility is Visibility.REPOSITORY
    assert promoted.status is ClaimStatus.PROMOTED


def _binding_with_integration() -> RevisionBinding:
    return RevisionBinding(
        repo_id="repo-1",
        branch="main",
        revision=REVISION,
        base_revision=BASE_REVISION,
        integration_ref="refs/heads/main",
        integration_sha="d" * 40,
    )


def test_promotion_survives_replay_without_double_mutation(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.CANONICAL)
    claim, receipt = _seeded_claim(ledger)
    ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.PASS,
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        validator="test-suite",
    )
    promoted, decision = ledger.promote(
        claim.claim_id, target=Visibility.REPOSITORY, attempt_succeeded=True,
        target_revision=REVISION, push_status="pushed",
        integration=_binding_with_integration(),
    )
    assert decision.allowed is True

    # Replaying the same promotion is a no-op on the stored record; the gate
    # reports the honest reason (repository is already the highest level).
    replayed, replay_decision = ledger.promote(
        claim.claim_id, target=Visibility.REPOSITORY, attempt_succeeded=True,
        target_revision=REVISION, push_status="pushed",
        integration=_binding_with_integration(),
    )
    assert replayed.visibility is Visibility.REPOSITORY
    assert replayed.record_version == promoted.record_version
    assert replay_decision.allowed is False
    assert "target_not_higher" in replay_decision.reasons
