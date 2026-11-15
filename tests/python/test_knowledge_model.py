"""Tests for the frozen knowledge-layer vocabulary (knowledge/model.py).

The model module is the contract every other knowledge module and the future
Rust facade shares, so these tests pin normalisation, fail-closed parsing and
the log-safe helpers rather than re-testing ledger behaviour.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from ultimate_coders.knowledge.model import (
    DEFAULT_EVIDENCE_TRUST,
    VERIFIED_EVIDENCE_KINDS,
    AttemptRef,
    ClaimStatus,
    EvidenceKind,
    KnowledgeScope,
    RevisionBinding,
    Visibility,
    canonical_json,
    default_trust_class,
    default_verified,
    digest,
    normalize_claim_status,
    normalize_evidence_kind,
    normalize_validation_result,
    normalize_visibility,
    safe_text,
    short_digest,
    utc_now,
    utc_now_iso,
    validation_result_passed,
    visibility_rank,
)


def test_visibility_normalises_documented_aliases():
    assert normalize_visibility("") is Visibility.ATTEMPT
    assert normalize_visibility("attempt-local") is Visibility.ATTEMPT
    assert normalize_visibility("task") is Visibility.GRAPH
    assert normalize_visibility("graph_scoped") is Visibility.GRAPH
    assert normalize_visibility("repo") is Visibility.REPOSITORY
    assert normalize_visibility(Visibility.REPOSITORY) is Visibility.REPOSITORY


def test_visibility_rejects_unknown_values_and_ranks_are_ordered():
    with pytest.raises(ValueError):
        normalize_visibility("planetary")
    assert visibility_rank("attempt") < visibility_rank("graph")
    assert visibility_rank("graph") < visibility_rank("repository")


def test_evidence_kind_aliases_and_trust_defaults():
    assert normalize_evidence_kind("diff") is EvidenceKind.PATCH_OBSERVED
    assert normalize_evidence_kind("outcome") is EvidenceKind.ATTEMPT_OUTCOME
    assert normalize_evidence_kind("merge") is EvidenceKind.MERGE_RECEIPT
    assert normalize_evidence_kind(EvidenceKind.TEST_VERDICT) is EvidenceKind.TEST_VERDICT
    with pytest.raises(ValueError):
        normalize_evidence_kind("telepathy")
    # Trust class is per-kind, never caller-invented by default.
    assert default_trust_class(EvidenceKind.PATCH_OBSERVED) == "git_diff"
    assert default_trust_class(EvidenceKind.ATTEMPT_OUTCOME) == "worker_reported"
    assert set(DEFAULT_EVIDENCE_TRUST) == set(EvidenceKind)
    # Machine-observed kinds are verified; model-reported ones are not.
    assert default_verified(EvidenceKind.PATCH_OBSERVED) is True
    assert default_verified(EvidenceKind.MERGE_RECEIPT) is True
    assert default_verified(EvidenceKind.ATTEMPT_OUTCOME) is False
    assert default_verified(EvidenceKind.FAILURE_SIGNATURE) is False
    assert default_verified(EvidenceKind.HYPOTHESIS) is False
    assert VERIFIED_EVIDENCE_KINDS <= set(EvidenceKind)


def test_claim_status_round_trip_and_unknown_status_fails():
    for status in ClaimStatus:
        assert normalize_claim_status(status.value) is status
    with pytest.raises(ValueError):
        normalize_claim_status("almost-validated")


def test_validation_result_normalisation_and_pass_only_truth():
    assert normalize_validation_result(True) is normalize_validation_result("passed")
    assert normalize_validation_result(False) is normalize_validation_result("failed")
    assert normalize_validation_result("unknown") is normalize_validation_result("")
    with pytest.raises(ValueError):
        normalize_validation_result("probably")
    # INCONCLUSIVE must never be treated as a pass.
    assert validation_result_passed("pass") is True
    assert validation_result_passed("ok") is True
    assert validation_result_passed("inconclusive") is False
    assert validation_result_passed("") is False
    assert validation_result_passed(False) is False


def test_attempt_ref_key_order_and_round_trip():
    attempt = AttemptRef(
        task_id="t1",
        subtask_id="s1",
        dispatch_attempt=2,
        attempt=1,
        worker_id="w1",
        worker_epoch=7,
        graph_id="g1",
    )
    assert attempt.key() == "t1:s1:2:1"
    assert attempt.order() == (2, 1)
    assert attempt.next_attempt().key() == "t1:s1:2:2"
    assert AttemptRef("t1", "s1", 2, 0).is_before(attempt) is True
    assert attempt.is_before(AttemptRef("t1", "s1", 2, 0)) is False

    restored = AttemptRef.from_dict(attempt.to_dict())
    assert restored == attempt
    # Tolerant parsing: missing and malformed numeric fields reset to zero.
    tolerant = AttemptRef.from_dict(
        {"task_id": "t1", "subtask_id": "s1", "attempt": "not-a-number"}
    )
    assert tolerant.attempt == 0
    assert tolerant.dispatch_attempt == 0


def test_attempt_ref_orders_across_dispatch_rounds():
    first_dispatch = AttemptRef("t1", "s1", dispatch_attempt=0, attempt=5)
    second_dispatch = AttemptRef("t1", "s1", dispatch_attempt=1, attempt=0)
    assert first_dispatch.order() < second_dispatch.order()


def test_knowledge_scope_read_rules():
    record = KnowledgeScope(tenant_id="t", repo_id="repo-a", branch="main", graph_id="g1")
    same = KnowledgeScope(tenant_id="t", repo_id="repo-a", branch="main", graph_id="g1")
    assert record.allows_read(same)
    assert record.allows_read(same, require_graph=True) is True

    # Cross-tenant is always denied.
    assert record.allows_read(
        KnowledgeScope(tenant_id="other", repo_id="repo-a", branch="main")
    ) is False
    # Pinned repo/branch never widen to an empty reader value.
    assert record.allows_read(KnowledgeScope(tenant_id="t", repo_id="repo-b")) is False
    assert (
        record.allows_read(
            KnowledgeScope(tenant_id="t", repo_id="repo-a", branch="dev")
        )
        is False
    )
    # Graph requirement needs both sides.
    assert record.allows_read(
        KnowledgeScope(tenant_id="t", repo_id="repo-a", branch="main"),
        require_graph=True,
    ) is False
    # Unpinned record fields act as reader wildcards.
    open_record = KnowledgeScope(tenant_id="t")
    assert open_record.allows_read(KnowledgeScope(tenant_id="t", repo_id="anything")) is True
    assert open_record.key() == "t::"


def test_revision_binding_applies_to_revision():
    pinned = RevisionBinding(revision="a" * 40, base_revision="b" * 40)
    assert pinned.applies_to("a" * 40) is True
    assert pinned.applies_to("a" * 12) is True
    assert pinned.applies_to("c" * 40) is False
    # A short SHA below the 7-char floor is not an unambiguous match.
    assert RevisionBinding(revision="abc1234").applies_to("abc") is False
    # Unpinned is a wildcard; a pinned binding refuses an empty candidate.
    assert RevisionBinding().applies_to("anything") is True
    assert pinned.applies_to("") is False


def test_revision_binding_integration_requires_both_fields():
    partial = RevisionBinding(integration_ref="refs/heads/main")
    complete = RevisionBinding(integration_ref="refs/heads/main", integration_sha="c" * 40)
    assert partial.has_integration is False
    assert complete.has_integration is True


def test_canonical_json_and_digest_are_deterministic():
    first = canonical_json({"b": 1, "a": [2, 3]})
    second = canonical_json({"a": [2, 3], "b": 1})
    assert first == second
    assert first == '{"a":[2,3],"b":1}'
    assert digest({"a": 1}) == digest({"a": 1})
    assert digest({"a": 1}) != digest({"a": 2})
    assert len(digest("x")) == 64


def test_short_digest_is_bounded():
    assert len(short_digest("x", length=8)) == 8
    assert len(short_digest("x", length=1)) == 4
    assert len(short_digest("x", length=1000)) == 64


def test_safe_text_removes_nul_and_marks_truncation():
    assert safe_text(None) == ""
    assert safe_text("plain") == "plain"
    assert safe_text("a\x00b") == "ab"
    clipped = safe_text("x" * 50, limit=10)
    assert clipped == "x" * 7 + "..."
    assert len(clipped) == 10
    # Below the marker width no ellipsis is possible; the clip is literal.
    assert safe_text("abcdef", limit=3) == "abc"
    assert safe_text("abcdef", limit=0) == ""


def test_utc_helpers_are_timezone_aware_iso_strings():
    assert utc_now().tzinfo is not None
    stamp = utc_now_iso()
    assert stamp.endswith("+00:00")
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
