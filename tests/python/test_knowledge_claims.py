"""Claim producers: the worker's bridge from evidence to gate-checked knowledge.

These tests pin the two claim shapes the worker now writes (failure and
delivery), the deterministic statements that make them replay-idempotent, and
the promotion helper's contract: validate with the merge receipt, then ask the
gate - never write the target visibility directly.
"""

from __future__ import annotations

import json

import pytest
from ultimate_coders.knowledge.claims import (
    ACCEPTED_MERGE_STATUSES,
    MAX_SIGNATURE_CHARS,
    attempt_scope,
    claim_summary,
    delivery_claim_statement,
    failure_claim_statement,
    promote_failure_claim,
    record_delivery_claim,
    record_failure_claim,
    validate_and_promote_delivery,
)
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.gate import REASON_ATTEMPT_STALE, REASON_GRAPH_SCOPE_MISSING
from ultimate_coders.knowledge.model import (
    AttemptRef,
    ClaimStatus,
    EvidenceKind,
    KnowledgeScope,
    Visibility,
)
from ultimate_coders.knowledge.rollout import KnowledgeStage
from ultimate_coders.runtime_state import RuntimeState

REVISION = "a" * 40


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("UC_KNOWLEDGE_STAGE", raising=False)


@pytest.fixture()
def state(tmp_path):
    return RuntimeState(tmp_path / "claims-state.sqlite3", database_url="")


def _attempt(attempt: int = 0) -> AttemptRef:
    return AttemptRef(
        task_id="task-1",
        subtask_id="sub-1",
        dispatch_attempt=0,
        attempt=attempt,
        worker_id="worker-a",
        worker_epoch=1,
        graph_id="g1",
    )


def _ledger(state, stage: KnowledgeStage = KnowledgeStage.GRAPH) -> EvidenceLedger:
    return EvidenceLedger(state, stage=stage)


def _receipts(ledger: EvidenceLedger, attempt: AttemptRef):
    outcome = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.ATTEMPT_OUTCOME,
        payload={"success": True},
        summary="attempt succeeded",
    )
    receipt = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.MERGE_RECEIPT,
        payload={"status": "merged", "commit_sha": REVISION},
        summary="workspace release",
    )
    return outcome, receipt


class TestAttemptScope:
    def test_scope_takes_graph_from_attempt_and_optional_branch(self):
        scope = attempt_scope(_attempt(), branch="main")

        assert scope.graph_id == "g1"
        assert scope.branch == "main"

    def test_scope_without_attempt_graph_stays_unpinned(self):
        attempt = AttemptRef(task_id="t", subtask_id="s", attempt=2)

        assert attempt_scope(attempt).graph_id == ""


class TestStatements:
    def test_failure_statement_names_attempt_subtask_and_signature(self):
        statement = failure_claim_statement(_attempt(attempt=2), "AssertionError: boom")

        assert statement == "attempt 2 of subtask sub-1 failed: AssertionError: boom"

    def test_failure_statement_never_invents_a_signature(self):
        assert failure_claim_statement(_attempt(), "") == (
            "attempt 0 of subtask sub-1 failed: no captured failure signature"
        )

    def test_failure_signature_is_bounded(self):
        statement = failure_claim_statement(_attempt(), "x" * 500)
        signature = statement.split("failed: ", 1)[1]

        assert len(signature) == MAX_SIGNATURE_CHARS
        assert signature.endswith("...")

    def test_delivery_statement_shortens_revision_and_counts_files(self):
        statement = delivery_claim_statement(
            _attempt(attempt=1),
            merge_status="merged",
            file_count=3,
            head_revision=REVISION,
            verification_label="review=approved",
        )

        assert "attempt 1 of subtask sub-1 delivered 3 file(s)" in statement
        assert "reported merged at revision aaaaaaaaa..." in statement
        assert "(verification: review=approved)" in statement

    def test_delivery_statement_marks_unknown_revision_honestly(self):
        statement = delivery_claim_statement(_attempt(), merge_status="merged")

        assert "at revision unknown-revision" in statement

    def test_unknown_verification_label_is_not_stated(self):
        assert "(verification" not in delivery_claim_statement(
            _attempt(), merge_status="merged", verification_label="unknown"
        )
        assert "(verification" not in delivery_claim_statement(
            _attempt(), merge_status="merged", verification_label=""
        )


class TestRecording:
    def test_failure_claim_is_attempt_scoped_and_links_evidence(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        outcome, receipt = _receipts(ledger, attempt)

        claim = record_failure_claim(
            ledger,
            attempt=attempt,
            scope=attempt_scope(attempt),
            evidence_ids=(outcome.evidence_id, receipt.evidence_id),
            signature="TimeoutError",
        )

        assert claim.status is ClaimStatus.CANDIDATE
        assert claim.visibility is Visibility.ATTEMPT
        assert "TimeoutError" in claim.statement
        assert claim.scope.graph_id == "g1"
        assert claim.evidence_ids == (outcome.evidence_id, receipt.evidence_id)

    def test_replaying_the_same_claim_does_not_overwrite(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        first = record_failure_claim(
            ledger, attempt=attempt, scope=attempt_scope(attempt), signature="same"
        )
        second = record_failure_claim(
            ledger, attempt=attempt, scope=attempt_scope(attempt), signature="same"
        )

        assert second.claim_id == first.claim_id
        assert second.record_version == first.record_version
        assert second.statement == first.statement

    def test_delivery_claim_binds_branch_and_revision(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        _outcome, receipt = _receipts(ledger, attempt)

        claim = record_delivery_claim(
            ledger,
            attempt=attempt,
            scope=attempt_scope(attempt, branch="main"),
            revision=REVISION,
            evidence_ids=(receipt.evidence_id,),
            merge_status="merged",
            file_count=2,
        )

        assert claim.binding.branch == "main"
        assert claim.binding.revision == REVISION
        assert claim.visibility is Visibility.ATTEMPT
        assert "delivered 2 file(s)" in claim.statement


class TestPromotion:
    def test_delivery_is_validated_by_its_receipt_then_promoted(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        outcome, receipt = _receipts(ledger, attempt)
        claim = record_delivery_claim(
            ledger,
            attempt=attempt,
            scope=attempt_scope(attempt, branch="main"),
            revision=REVISION,
            evidence_ids=(outcome.evidence_id, receipt.evidence_id),
            merge_status="merged",
            file_count=1,
        )

        promoted, decision = validate_and_promote_delivery(
            ledger,
            claim.claim_id,
            revision=REVISION,
            evidence_ids=(receipt.evidence_id,),
            target=Visibility.GRAPH,
        )

        assert decision.allowed is True
        assert promoted.visibility is Visibility.GRAPH
        assert promoted.status is ClaimStatus.PROMOTED
        validation = promoted.latest_validation()
        assert validation is not None and validation.result.value == "pass"
        assert validation.strategy == "merge-receipt"
        assert validation.validator == "worker"

    def test_promotion_without_a_graph_scope_is_denied_without_mutation(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        outcome, receipt = _receipts(ledger, attempt)
        claim = record_delivery_claim(
            ledger,
            attempt=attempt,
            scope=KnowledgeScope(branch="main"),
            revision=REVISION,
            evidence_ids=(outcome.evidence_id, receipt.evidence_id),
            merge_status="merged",
        )

        promoted, decision = validate_and_promote_delivery(
            ledger, claim.claim_id, revision=REVISION, evidence_ids=(receipt.evidence_id,)
        )

        assert decision.denied is True
        assert REASON_GRAPH_SCOPE_MISSING in decision.reasons
        assert promoted.visibility is Visibility.ATTEMPT
        # Only the validation was written; the promotion denial changed nothing
        # about the claim's status or audience.
        after = ledger.get_claim(claim.claim_id)
        assert after is not None
        assert after.visibility is Visibility.ATTEMPT
        # Validation marks the claim VALIDATED, but the denied promotion must
        # not have moved it to PROMOTED.
        assert after.status is not ClaimStatus.PROMOTED

    def test_stale_attempt_is_denied_even_when_validated(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        outcome, receipt = _receipts(ledger, attempt)
        claim = record_delivery_claim(
            ledger,
            attempt=attempt,
            scope=attempt_scope(attempt),
            revision=REVISION,
            evidence_ids=(outcome.evidence_id, receipt.evidence_id),
            merge_status="merged",
        )

        _promoted, decision = validate_and_promote_delivery(
            ledger,
            claim.claim_id,
            revision=REVISION,
            evidence_ids=(receipt.evidence_id,),
            attempt_stale=True,
        )

        assert decision.denied is True
        assert REASON_ATTEMPT_STALE in decision.reasons

    def test_failure_claim_is_never_promoted_by_the_worker(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        outcome, _receipt = _receipts(ledger, attempt)
        claim = record_failure_claim(
            ledger,
            attempt=attempt,
            scope=attempt_scope(attempt),
            evidence_ids=(outcome.evidence_id,),
            signature="boom",
        )

        promoted, decision = promote_failure_claim(ledger, claim.claim_id)

        assert decision.denied is True
        assert promoted.visibility is Visibility.ATTEMPT
        assert promoted.status is ClaimStatus.CANDIDATE

    def test_accepted_merge_statuses_are_the_documented_set(self):
        assert ACCEPTED_MERGE_STATUSES == frozenset({"merged", "already_merged", "no_changes"})


class TestClaimSummary:
    def test_summary_is_log_safe(self, state):
        ledger = _ledger(state)
        attempt = _attempt()
        _receipts(ledger, attempt)
        claim = record_failure_claim(
            ledger,
            attempt=attempt,
            scope=attempt_scope(attempt),
            signature="SECRET failure detail",
        )
        _promoted, decision = promote_failure_claim(ledger, claim.claim_id)

        summary = claim_summary(claim, decision)
        encoded = json.dumps(summary)

        assert summary["claim_id"] == claim.claim_id
        assert summary["status"] == "candidate"
        assert summary["allowed"] is False
        assert "reasons" in summary
        assert "SECRET" not in encoded
        assert "failed:" not in encoded
