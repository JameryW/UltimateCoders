"""Contract tests for the epistemic gate: pure read/promotion rules, no I/O.

The gate is the single vocabulary the ledger, recovery view and worker manifest
switch on, so these tests pin the exported reason codes, the read rules for the
three visibility levels and the promotion ordering. They never touch
``RuntimeState`` - that separation is part of the contract.
"""

from __future__ import annotations

import json

import pytest
from ultimate_coders.knowledge.gate import (
    ALL_REASON_CODES,
    REASON_ATTEMPT_CHAIN_REQUIRED,
    REASON_ATTEMPT_NOT_SUCCESSFUL,
    REASON_ATTEMPT_STALE,
    REASON_CLAIM_TERMINAL,
    REASON_CROSS_SCOPE_DENIED,
    REASON_GRAPH_NOT_SHARED,
    REASON_GRAPH_SCOPE_MISSING,
    REASON_INTEGRATION_MISSING,
    REASON_PUSH_UNKNOWN,
    REASON_REVISION_MISMATCH,
    REASON_STAGE_DISABLED,
    REASON_TARGET_NOT_HIGHER,
    REASON_VALIDATION_FAILED,
    REASON_VALIDATION_INCONCLUSIVE,
    REASON_VALIDATION_MISSING,
    EpistemicGate,
    GateDecision,
    PromotionRequest,
)
from ultimate_coders.knowledge.model import (
    ClaimStatus,
    KnowledgeScope,
    RevisionBinding,
    ValidationResult,
    Visibility,
)

GATE = EpistemicGate()


def _scope(**overrides) -> KnowledgeScope:
    values = {
        "tenant_id": "tenant-a",
        "repo_id": "repo-1",
        "branch": "main",
        "graph_id": "graph-1",
    }
    values.update(overrides)
    return KnowledgeScope(**values)


def _integration(**overrides) -> RevisionBinding:
    values = {
        "repo_id": "repo-1",
        "branch": "main",
        "revision": "abc1234",
        "integration_ref": "refs/heads/main",
        "integration_sha": "abc1234",
    }
    values.update(overrides)
    return RevisionBinding(**values)


def _graph_request(**overrides) -> PromotionRequest:
    values = {
        "claim_visibility": Visibility.ATTEMPT,
        "target_visibility": Visibility.GRAPH,
        "scope": _scope(),
        "attempt_succeeded": True,
        "validation_result": ValidationResult.PASS,
        "validation_revision": "abc1234",
        "target_revision": "abc1234",
    }
    values.update(overrides)
    return PromotionRequest(**values)


def _repository_request(**overrides) -> PromotionRequest:
    values = {
        "claim_visibility": Visibility.GRAPH,
        "target_visibility": Visibility.REPOSITORY,
        "scope": _scope(),
        "attempt_succeeded": True,
        "validation_result": ValidationResult.PASS,
        "validation_revision": "abc1234",
        "target_revision": "abc1234",
        "integration": _integration(),
        "push_status": "pushed",
    }
    values.update(overrides)
    return PromotionRequest(**values)


# ---------------------------------------------------------------------------
# Reason vocabulary and decision objects
# ---------------------------------------------------------------------------


def test_reason_vocabulary_is_stable_and_exported():
    assert REASON_TARGET_NOT_HIGHER == "target_not_higher"
    assert REASON_ATTEMPT_NOT_SUCCESSFUL == "attempt_not_successful"
    assert REASON_ATTEMPT_STALE == "attempt_stale"
    assert REASON_VALIDATION_MISSING == "validation_missing"
    assert REASON_VALIDATION_FAILED == "validation_failed"
    assert REASON_VALIDATION_INCONCLUSIVE == "validation_inconclusive"
    assert REASON_REVISION_MISMATCH == "revision_mismatch"
    assert REASON_GRAPH_SCOPE_MISSING == "graph_scope_missing"
    assert REASON_INTEGRATION_MISSING == "integration_missing"
    assert REASON_PUSH_UNKNOWN == "push_unknown"
    assert REASON_CLAIM_TERMINAL == "claim_terminal"
    assert REASON_CROSS_SCOPE_DENIED == "cross_scope_denied"
    assert REASON_GRAPH_NOT_SHARED == "graph_not_shared"
    assert REASON_ATTEMPT_CHAIN_REQUIRED == "attempt_chain_required"
    assert REASON_STAGE_DISABLED == "stage_disabled"
    assert len(ALL_REASON_CODES) == 15
    assert len(set(ALL_REASON_CODES)) == 15


def test_gate_decision_dedupes_reasons_and_is_json_safe():
    decision = GateDecision(
        allowed=False,
        visibility=Visibility.GRAPH,
        reasons=(REASON_ATTEMPT_STALE, REASON_ATTEMPT_STALE, REASON_VALIDATION_MISSING),
    )
    assert decision.denied is True
    assert decision.allowed is False
    assert decision.reasons == (REASON_ATTEMPT_STALE, REASON_VALIDATION_MISSING)
    assert decision.visibility is Visibility.GRAPH
    payload = decision.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["visibility"] == "graph"
    assert payload["denied"] is True
    assert decision.summary()["reason_count"] == 2


def test_gate_decision_normalizes_legacy_visibility_values():
    decision = GateDecision(allowed=1, visibility="repo", reasons=())
    assert decision.allowed is True
    assert decision.visibility is Visibility.REPOSITORY


# ---------------------------------------------------------------------------
# Read rules: attempt-scoped records
# ---------------------------------------------------------------------------


def test_attempt_record_requires_same_retry_chain():
    decision = GATE.can_read(
        record_visibility=Visibility.ATTEMPT,
        record_scope=_scope(),
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_ATTEMPT_CHAIN_REQUIRED,)
    assert decision.visibility is Visibility.ATTEMPT

    allowed = GATE.can_read(
        record_visibility=Visibility.ATTEMPT,
        record_scope=_scope(),
        same_chain=True,
    )
    assert allowed.allowed is True
    assert allowed.reasons == ()


def test_attempt_record_ignores_scope_and_revision():
    decision = GATE.can_read(
        record_visibility=Visibility.ATTEMPT,
        record_scope=_scope(tenant_id="tenant-a", repo_id="repo-1", branch="main"),
        reader_visibility=Visibility.ATTEMPT,
        reader_scope=_scope(tenant_id="tenant-b", repo_id="other", branch="dev"),
        binding=RevisionBinding(revision="abc1234"),
        reader_revision="zzz9999",
        same_chain=True,
    )
    assert decision.allowed is True
    assert decision.visibility is Visibility.ATTEMPT


# ---------------------------------------------------------------------------
# Read rules: graph-scoped records
# ---------------------------------------------------------------------------


def test_graph_record_readable_by_matching_graph_reader():
    decision = GATE.can_read(
        record_visibility=Visibility.GRAPH,
        record_scope=_scope(graph_id="graph-1"),
        reader_visibility=Visibility.GRAPH,
        reader_scope=_scope(graph_id="graph-1"),
    )
    assert decision.allowed is True
    assert decision.visibility is Visibility.GRAPH


def test_graph_record_requires_shared_graph_identity():
    decision = GATE.can_read(
        record_visibility=Visibility.GRAPH,
        record_scope=_scope(graph_id="graph-1"),
        reader_visibility=Visibility.GRAPH,
        reader_scope=_scope(graph_id="graph-2"),
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_GRAPH_NOT_SHARED,)

    empty_reader = GATE.can_read(
        record_visibility=Visibility.GRAPH,
        record_scope=_scope(graph_id="graph-1"),
        reader_visibility=Visibility.GRAPH,
        reader_scope=_scope(graph_id=""),
    )
    assert empty_reader.reasons == (REASON_GRAPH_NOT_SHARED,)


def test_graph_record_requires_compatible_tenant_repo_branch():
    decision = GATE.can_read(
        record_visibility=Visibility.GRAPH,
        record_scope=_scope(tenant_id="tenant-a"),
        reader_visibility=Visibility.GRAPH,
        reader_scope=_scope(tenant_id="tenant-b"),
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_CROSS_SCOPE_DENIED, REASON_GRAPH_NOT_SHARED)


def test_graph_record_requires_graph_level_reader():
    decision = GATE.can_read(
        record_visibility=Visibility.GRAPH,
        record_scope=_scope(graph_id="graph-1"),
        reader_visibility=Visibility.ATTEMPT,
        reader_scope=_scope(graph_id="graph-1"),
    )
    assert decision.denied is True
    assert REASON_CROSS_SCOPE_DENIED in decision.reasons


# ---------------------------------------------------------------------------
# Read rules: repository-scoped records
# ---------------------------------------------------------------------------


def test_repository_record_needs_repository_reader_and_scope():
    denied = GATE.can_read(
        record_visibility=Visibility.REPOSITORY,
        record_scope=_scope(),
        reader_visibility=Visibility.GRAPH,
        reader_scope=_scope(),
    )
    assert denied.denied is True
    assert denied.reasons == (REASON_CROSS_SCOPE_DENIED,)

    allowed = GATE.can_read(
        record_visibility=Visibility.REPOSITORY,
        record_scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        reader_scope=_scope(),
    )
    assert allowed.allowed is True
    assert allowed.visibility is Visibility.REPOSITORY


def test_repository_record_denies_cross_scope_reader():
    decision = GATE.can_read(
        record_visibility=Visibility.REPOSITORY,
        record_scope=_scope(branch="main"),
        reader_visibility=Visibility.REPOSITORY,
        reader_scope=_scope(branch="dev"),
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_CROSS_SCOPE_DENIED,)


def test_repository_record_denies_pinned_revision_mismatch():
    binding = RevisionBinding(revision="abcdef1")
    mismatch = GATE.can_read(
        record_visibility=Visibility.REPOSITORY,
        record_scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        binding=binding,
        reader_revision="1234567",
    )
    assert mismatch.denied is True
    assert mismatch.reasons == (REASON_REVISION_MISMATCH,)

    missing_revision = GATE.can_read(
        record_visibility=Visibility.REPOSITORY,
        record_scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        binding=binding,
    )
    assert missing_revision.reasons == (REASON_REVISION_MISMATCH,)


def test_repository_record_accepts_matching_or_absent_binding():
    prefix_match = GATE.can_read(
        record_visibility=Visibility.REPOSITORY,
        record_scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        binding=RevisionBinding(revision="abc1234"),
        reader_revision="abc1234deadbeef",
    )
    assert prefix_match.allowed is True

    wildcard = GATE.can_read(
        record_visibility=Visibility.REPOSITORY,
        record_scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        binding=None,
        reader_revision="anything",
    )
    assert wildcard.allowed is True


# ---------------------------------------------------------------------------
# Promotion rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [ClaimStatus.SUPERSEDED, ClaimStatus.RETRACTED])
def test_terminal_claims_can_never_be_promoted(status):
    decision = GATE.evaluate_promotion(_graph_request(status=status))
    assert decision.denied is True
    assert decision.reasons == (REASON_CLAIM_TERMINAL,)
    assert decision.visibility is Visibility.ATTEMPT


def test_target_must_be_strictly_higher_than_claim():
    decision = GATE.evaluate_promotion(
        _graph_request(claim_visibility=Visibility.GRAPH, target_visibility=Visibility.GRAPH)
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_TARGET_NOT_HIGHER,)
    assert decision.visibility is Visibility.GRAPH


def test_graph_promotion_requires_successful_fresh_attempt_and_validation():
    decision = GATE.evaluate_promotion(
        _graph_request(
            attempt_succeeded=False,
            attempt_stale=True,
            validation_result=None,
        )
    )
    assert decision.denied is True
    assert decision.reasons == (
        REASON_ATTEMPT_NOT_SUCCESSFUL,
        REASON_ATTEMPT_STALE,
        REASON_VALIDATION_MISSING,
    )
    assert decision.visibility is Visibility.ATTEMPT


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (ValidationResult.FAIL, REASON_VALIDATION_FAILED),
        (ValidationResult.INCONCLUSIVE, REASON_VALIDATION_INCONCLUSIVE),
    ],
)
def test_graph_promotion_rejects_non_passing_validation(result, reason):
    decision = GATE.evaluate_promotion(_graph_request(validation_result=result))
    assert decision.denied is True
    assert decision.reasons == (reason,)


def test_graph_promotion_allows_validated_fresh_attempt():
    decision = GATE.evaluate_promotion(_graph_request())
    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.visibility is Visibility.GRAPH


def test_graph_promotion_checks_revision_and_graph_scope():
    mismatch = GATE.evaluate_promotion(
        _graph_request(validation_revision="abcdef1", target_revision="1234567")
    )
    assert mismatch.reasons == (REASON_REVISION_MISMATCH,)

    missing_scope = GATE.evaluate_promotion(_graph_request(scope=_scope(graph_id="")))
    assert missing_scope.reasons == (REASON_GRAPH_SCOPE_MISSING,)


def test_short_revision_prefixes_below_seven_chars_do_not_match():
    decision = GATE.evaluate_promotion(
        _graph_request(validation_revision="abc123", target_revision="abc1234")
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_REVISION_MISMATCH,)


def test_repository_promotion_requires_integration_receipt():
    decision = GATE.evaluate_promotion(_repository_request(integration=None))
    assert decision.denied is True
    assert decision.reasons == (REASON_INTEGRATION_MISSING,)

    incomplete = GATE.evaluate_promotion(
        _repository_request(integration=RevisionBinding(branch="main", revision="abc1234"))
    )
    assert incomplete.reasons == (REASON_INTEGRATION_MISSING,)


def test_repository_promotion_rejects_unknown_push_status():
    decision = GATE.evaluate_promotion(_repository_request(push_status=" unknown "))
    assert decision.denied is True
    assert decision.reasons == (REASON_PUSH_UNKNOWN,)


def test_repository_promotion_checks_integration_revision():
    decision = GATE.evaluate_promotion(
        _repository_request(integration=_integration(revision="1234567"))
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_REVISION_MISMATCH,)


def test_repository_promotion_allows_complete_receipt():
    decision = GATE.evaluate_promotion(_repository_request())
    assert decision.allowed is True
    assert decision.visibility is Visibility.REPOSITORY
    assert json.loads(json.dumps(decision.to_dict())) == decision.to_dict()


def test_promotion_request_normalizes_inputs_without_mutating():
    request = PromotionRequest(
        claim_visibility="",
        target_visibility="canonical",
        scope={"tenant_id": "tenant-a", "graph_id": "graph-1"},
        status="candidate",
        validation_result="",
    )
    assert request.claim_visibility is Visibility.ATTEMPT
    assert request.target_visibility is Visibility.REPOSITORY
    assert request.scope.tenant_id == "tenant-a"
    assert request.status is ClaimStatus.CANDIDATE
    assert request.validation_result is ValidationResult.INCONCLUSIVE
    before = json.dumps(request.to_dict(), sort_keys=True)
    GATE.evaluate_promotion(request)
    assert json.dumps(request.to_dict(), sort_keys=True) == before
