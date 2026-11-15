"""Tests for the budgeted graph-knowledge read face (knowledge/retrieval.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.model import (
    AttemptRef,
    ClaimStatus,
    EvidenceKind,
    KnowledgeScope,
    RevisionBinding,
    Visibility,
)
from ultimate_coders.knowledge.retrieval import (
    RETRIEVAL_SOURCE,
    _bitemporal_project,
    claims_to_context_items,
    knowledge_context_items,
    retrieve_claims,
)
from ultimate_coders.knowledge.rollout import KnowledgeStage
from ultimate_coders.runtime_state import RuntimeState

GRAPH_SCOPE = KnowledgeScope(graph_id="g1", branch="main")
REVISION = "a" * 40


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("UC_KNOWLEDGE_STAGE", raising=False)
    monkeypatch.delenv("UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS", raising=False)


@pytest.fixture()
def state(tmp_path):
    return RuntimeState(tmp_path / "retrieval-state.sqlite3", database_url="")


def _attempt(graph_id: str = "g1", attempt: int = 0) -> AttemptRef:
    return AttemptRef(
        task_id="t1",
        subtask_id="s1",
        dispatch_attempt=0,
        attempt=attempt,
        worker_id="w-test",
        graph_id=graph_id,
    )


def _promoted_claim(
    ledger: EvidenceLedger,
    *,
    statement: str,
    scope: KnowledgeScope = GRAPH_SCOPE,
    confidence: float = 0.5,
    binding: RevisionBinding | None = None,
):
    attempt = _attempt(graph_id=scope.graph_id or "g1")
    evidence = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.ATTEMPT_OUTCOME,
        payload={"success": True},
        summary="attempt outcome",
    )
    claim = ledger.record_claim(
        statement=statement,
        scope=scope,
        attempt=attempt,
        evidence_ids=(evidence.evidence_id,),
        binding=binding or RevisionBinding(branch="main", revision=REVISION),
        confidence=confidence,
    )
    ledger.validate_claim(claim.claim_id, result="pass", revision=REVISION)
    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.GRAPH,
        attempt_succeeded=True,
        target_revision=REVISION,
    )
    assert decision.allowed is True
    return promoted


def test_read_face_is_off_before_stage_graph(state):
    # A promoted claim exists, but a reader in an earlier stage must not see it.
    promoting = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    _promoted_claim(promoting, statement="a promoted delivery")
    reading = EvidenceLedger(state, stage=KnowledgeStage.RECOVERY)

    claims = retrieve_claims(reading, scope=GRAPH_SCOPE)

    assert claims == []


def test_promoted_claim_is_visible_to_a_graph_reader(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    _promoted_claim(ledger, statement="attempt 2 delivered 2 file(s)", confidence=0.75)

    claims = retrieve_claims(ledger, scope=GRAPH_SCOPE)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.statement == "attempt 2 delivered 2 file(s)"
    assert claim.visibility is Visibility.GRAPH
    assert claim.status.value == "promoted"
    assert claim.confidence == 0.75


def test_bitemporal_projection_keeps_the_strongest_duplicate(monkeypatch):
    monkeypatch.setenv("UC_BITEMPORAL_KNOWLEDGE", "1")
    weak = SimpleNamespace(
        claim_id="weak",
        statement="duplicate claim",
        visibility=Visibility.GRAPH,
        status=ClaimStatus.CANDIDATE,
        confidence=0.3,
    )
    strong = SimpleNamespace(
        claim_id="strong",
        statement="duplicate claim",
        visibility=Visibility.GRAPH,
        status=ClaimStatus.PROMOTED,
        confidence=0.9,
    )

    projected = _bitemporal_project([weak, strong], "")

    assert [claim.claim_id for claim in projected] == ["strong"]


def test_bitemporal_projection_is_opt_in(monkeypatch):
    monkeypatch.delenv("UC_BITEMPORAL_KNOWLEDGE", raising=False)
    weak = SimpleNamespace(
        claim_id="weak",
        statement="duplicate claim",
        visibility=Visibility.GRAPH,
        status=ClaimStatus.CANDIDATE,
        confidence=0.3,
    )
    strong = SimpleNamespace(
        claim_id="strong",
        statement="duplicate claim",
        visibility=Visibility.GRAPH,
        status=ClaimStatus.PROMOTED,
        confidence=0.9,
    )

    projected = _bitemporal_project([weak, strong], "")

    assert [claim.claim_id for claim in projected] == ["weak", "strong"]


def test_attempt_scoped_claim_never_reaches_the_read_face(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    attempt = _attempt()
    evidence = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.ATTEMPT_OUTCOME,
        payload={"success": True},
    )
    ledger.record_claim(
        statement="private attempt note",
        scope=GRAPH_SCOPE,
        attempt=attempt,
        evidence_ids=(evidence.evidence_id,),
    )

    assert retrieve_claims(ledger, scope=GRAPH_SCOPE) == []


def test_other_graph_scope_is_not_visible(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    _promoted_claim(
        ledger,
        statement="belongs to another graph",
        scope=KnowledgeScope(graph_id="g2", branch="main"),
    )

    assert retrieve_claims(ledger, scope=GRAPH_SCOPE) == []
    assert (
        len(retrieve_claims(ledger, scope=KnowledgeScope(graph_id="g2", branch="main"))) == 1
    )


def test_revision_binding_filters_stale_readers(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    _promoted_claim(ledger, statement="bound to revision a")

    assert retrieve_claims(ledger, scope=GRAPH_SCOPE, reader_revision="b" * 40) == []
    # An empty reader revision means "revision not pinned", which the binding
    # treats as a wildcard read.
    assert len(retrieve_claims(ledger, scope=GRAPH_SCOPE, reader_revision="")) == 1


def test_ranked_by_confidence(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    _promoted_claim(ledger, statement="low confidence", confidence=0.2)
    _promoted_claim(ledger, statement="high confidence", confidence=0.9)

    claims = retrieve_claims(ledger, scope=GRAPH_SCOPE)

    assert [claim.statement for claim in claims] == ["high confidence", "low confidence"]


def test_items_are_evidence_tier_and_verified(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    _promoted_claim(ledger, statement="delivered at revision a")

    items, summary = knowledge_context_items(ledger, scope=GRAPH_SCOPE)

    assert len(items) == 1
    item = items[0]
    assert item.tier.name == "EVIDENCE"
    assert item.source == RETRIEVAL_SOURCE
    assert item.trust_class == "knowledge_claim"
    assert item.verified is True
    assert item.source_version == "promoted"
    assert "delivered at revision a" in item.content
    assert summary["claims_used"] == 1
    assert summary["stage"] == "graph"
    assert summary["truncated"] is False


def test_budget_skips_claims_that_do_not_fit(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    statement = "x" * 180
    for index in range(8):
        _promoted_claim(ledger, statement=f"{index} {statement}", confidence=0.5)

    items, summary = knowledge_context_items(ledger, scope=GRAPH_SCOPE, budget_tokens=128)

    assert summary["claims_available"] == 8
    assert summary["claims_used"] < summary["claims_available"]
    assert summary["tokens"] <= 128
    assert summary["truncated"] is True
    assert all(item.token_cost is not None for item in items)


def test_summary_is_log_safe(state):
    ledger = EvidenceLedger(state, stage=KnowledgeStage.GRAPH)
    _promoted_claim(ledger, statement="SECRET claim statement")

    _items, summary = knowledge_context_items(ledger, scope=GRAPH_SCOPE)
    encoded = json.dumps(summary)

    assert "SECRET" not in encoded
    assert set(summary) == {
        "claims_available",
        "claims_used",
        "tokens",
        "budget_tokens",
        "truncated",
        "stage",
        "claims_visible",
    }


def test_packing_never_splits_a_claim():
    claim = type(
        "Claim",
        (),
        {
            "claim_id": "c1",
            "statement": "y" * 2000,
            "visibility": Visibility.GRAPH,
            "status": type("Status", (), {"value": "promoted"})(),
            "confidence": 0.5,
        },
    )()
    items, summary = claims_to_context_items([claim], budget_tokens=128)

    assert items == []
    assert summary["truncated"] is True
