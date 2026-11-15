"""Contract tests for the evidence ledger and its epistemic gate integration.

These use a real ``RuntimeState`` on a temporary SQLite database (never a shared
file), because the ledger's contract is about persistence: replay idempotency,
bounded/redacted payloads, attempt-scoped evidence, append-only validation
history, and promotions that either mutate exactly once or not at all.
"""

from __future__ import annotations

import json

import pytest
from ultimate_coders.knowledge.evidence import (
    CLAIM_NAMESPACE,
    NAMESPACE,
    ClaimRecord,
    EvidenceLedger,
    EvidenceRecord,
    ValidationRecord,
)
from ultimate_coders.knowledge.gate import (
    REASON_ATTEMPT_NOT_SUCCESSFUL,
    REASON_CLAIM_TERMINAL,
    REASON_GRAPH_SCOPE_MISSING,
    REASON_INTEGRATION_MISSING,
    REASON_PUSH_UNKNOWN,
    REASON_REVISION_MISMATCH,
    REASON_STAGE_DISABLED,
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
    canonical_json,
)
from ultimate_coders.knowledge.rollout import KnowledgeStage
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture
def state(tmp_path) -> RuntimeState:
    return RuntimeState(tmp_path / "state.db", database_url="")


def _ledger(
    state: RuntimeState,
    stage: KnowledgeStage = KnowledgeStage.CANONICAL,
) -> EvidenceLedger:
    return EvidenceLedger(state, stage=stage)


def _attempt(**overrides) -> AttemptRef:
    values = {
        "task_id": "task-1",
        "subtask_id": "sub-1",
        "dispatch_attempt": 0,
        "attempt": 1,
        "worker_id": "worker-a",
        "worker_epoch": 1,
        "graph_id": "graph-1",
    }
    values.update(overrides)
    return AttemptRef(**values)


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


def _validated_claim(
    ledger: EvidenceLedger,
    *,
    statement: str = "claim under test",
    visibility: Visibility = Visibility.ATTEMPT,
    scope: KnowledgeScope = None,
    binding: RevisionBinding = None,
    revision: str = "abc1234",
) -> ClaimRecord:
    claim = ledger.record_claim(
        statement=statement,
        scope=scope or _scope(),
        attempt=_attempt(),
        evidence_ids=("ev-1",),
        binding=binding,
        visibility=visibility,
    )
    claim, _ = ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.PASS,
        revision=revision,
        strategy="pytest",
        validator="worker-a",
    )
    return claim


# ---------------------------------------------------------------------------
# Evidence capture
# ---------------------------------------------------------------------------


def test_record_evidence_defaults_and_replay_is_idempotent(state):
    ledger = _ledger(state)
    record = ledger.record_evidence(
        attempt=_attempt(),
        kind=EvidenceKind.TEST_VERDICT,
        payload={"passed": True, "tests": 12},
        summary="pytest green",
    )
    assert record.kind is EvidenceKind.TEST_VERDICT
    assert record.trust_class == "test_runner"
    assert record.verified is True
    assert record.visibility is Visibility.ATTEMPT
    assert record.scope.graph_id == "graph-1"
    assert record.created_at
    assert record.record_version == 1
    assert record.key() == f"{_attempt().key()}:{record.evidence_id}"
    assert record.tokens() > 0

    stored = state.versioned_get(NAMESPACE, record.evidence_id)
    assert stored is not None
    assert stored.version == 1

    replay = ledger.record_evidence(
        attempt=_attempt(),
        kind=EvidenceKind.TEST_VERDICT,
        payload={"tests": 12, "passed": True},
        summary="pytest green",
    )
    assert replay.evidence_id == record.evidence_id
    assert replay == record
    assert len(ledger.evidence_for(_attempt())) == 1
    assert ledger.get_evidence(record.evidence_id) == record
    assert ledger.get_evidence("missing-evidence") is None


def test_evidence_redacts_prompt_and_diff_material(state):
    ledger = _ledger(state)
    prompt = "secret prompt " * 400
    record = ledger.record_evidence(
        attempt=_attempt(),
        kind=EvidenceKind.HYPOTHESIS,
        payload={
            "prompt": prompt,
            "system_prompt": {"text": prompt},
            "diff": "--- a/x.py\n+++ b/x.py\n+secret\n",
            "note": "kept",
        },
    )
    assert record.payload["prompt"]["redacted"] is True
    assert record.payload["prompt"]["chars"] > 4000
    assert "digest" in record.payload["prompt"]
    assert record.payload["diff"]["redacted"] is True
    assert record.payload["note"] == "kept"
    rendered = canonical_json(record.payload)
    assert "secret prompt" not in rendered
    assert "--- a/x.py" not in rendered


def test_evidence_payload_is_bounded_and_json_safe(state):
    ledger = _ledger(state)
    record = ledger.record_evidence(
        attempt=_attempt(),
        kind=EvidenceKind.PATCH_OBSERVED,
        payload={
            "files": [f"file-{index}.py" for index in range(500)],
            "meta": "m" * 10_000,
            "flag": None,
            "score": 1.5,
        },
    )
    assert len(record.payload["files"]) == 50
    assert len(record.payload["meta"]) <= 2_003
    assert record.payload["flag"] is None
    assert record.payload["score"] == 1.5
    assert json.loads(json.dumps(record.payload)) == record.payload


def test_oversized_evidence_payload_becomes_digest_envelope(state):
    ledger = _ledger(state)
    payload = {f"field-{index}": "v" * 1_000 for index in range(30)}
    record = ledger.record_evidence(
        attempt=_attempt(),
        kind=EvidenceKind.PATCH_OBSERVED,
        payload=payload,
    )
    assert record.payload["truncated"] is True
    assert "payload_digest" in record.payload
    assert record.payload["chars"] > 8_000
    assert len(record.payload["preview"]) <= 1_004
    assert "v" * 1_000 not in json.dumps(record.payload)


def test_evidence_for_is_attempt_scoped_and_filtered(state):
    ledger = _ledger(state)
    first = _attempt(attempt=1)
    second = _attempt(attempt=2)
    verdict = ledger.record_evidence(
        attempt=first, kind=EvidenceKind.TEST_VERDICT, payload={"index": 1}
    )
    ledger.record_evidence(
        attempt=first, kind=EvidenceKind.PATCH_OBSERVED, payload={"file": "x.py"}
    )
    ledger.record_evidence(
        attempt=second, kind=EvidenceKind.TEST_VERDICT, payload={"index": 2}
    )

    filtered = ledger.evidence_for(first, kind=EvidenceKind.TEST_VERDICT)
    assert [item.evidence_id for item in filtered] == [verdict.evidence_id]
    assert len(ledger.evidence_for(first)) == 2
    assert len(ledger.evidence_for(second)) == 1
    assert len(ledger.evidence_for(first, limit=1)) == 1
    assert ledger.evidence_for(first, limit=0) == []


# ---------------------------------------------------------------------------
# Claims and validations
# ---------------------------------------------------------------------------


def test_record_claim_is_deterministic_and_never_overwritten(state):
    ledger = _ledger(state)
    claim = ledger.record_claim(
        statement="  Prefer   Early   Returns ",
        scope=_scope(),
        attempt=_attempt(),
        evidence_ids=("ev-1", "ev-1"),
        confidence=1.7,
    )
    assert claim.statement == "Prefer Early Returns"
    assert claim.confidence == 1.0
    assert claim.evidence_ids == ("ev-1",)
    assert claim.status is ClaimStatus.CANDIDATE
    assert claim.record_version == 1

    replay = ledger.record_claim(
        statement="prefer early returns",
        scope=_scope(),
        confidence=0.1,
    )
    assert replay.claim_id == claim.claim_id
    assert replay.confidence == 1.0
    assert replay.statement == "Prefer Early Returns"

    other_scope = ledger.record_claim(statement="prefer early returns", scope=_scope(branch="dev"))
    assert other_scope.claim_id != claim.claim_id

    with pytest.raises(ValueError):
        ledger.record_claim(statement="   ", scope=_scope())


def test_validation_requires_known_claim_and_pass_marks_validated(state):
    ledger = _ledger(state)
    with pytest.raises(ValueError):
        ledger.validate_claim("missing-claim", result=ValidationResult.PASS)

    claim = ledger.record_claim(statement="validation target", scope=_scope())
    claim, validation = ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.PASS,
        revision="abc1234",
        evidence_ids=("ev-1", "ev-1"),
        strategy="pytest",
        validator="worker-a",
    )
    assert claim.status is ClaimStatus.VALIDATED
    assert claim.record_version == 2
    assert claim.latest_validation() == validation
    assert validation.result is ValidationResult.PASS
    assert validation.evidence_ids == ("ev-1",)

    replay, existing = ledger.validate_claim(
        claim.claim_id,
        result=ValidationResult.PASS,
        revision="abc1234",
        evidence_ids=("ev-1",),
        strategy="pytest",
        validator="worker-a",
        notes="different prose is not part of the digest",
    )
    assert existing.validation_id == validation.validation_id
    assert replay.record_version == claim.record_version
    stored = ledger.get_claim(claim.claim_id)
    assert len(stored.validations) == 1


def test_failed_validation_keeps_claim_candidate_and_blocks_promotion(state):
    ledger = _ledger(state)
    claim = ledger.record_claim(statement="failure path", scope=_scope())
    claim, validation = ledger.validate_claim(claim.claim_id, result=ValidationResult.FAIL)
    assert validation.result is ValidationResult.FAIL
    assert claim.status is ClaimStatus.CANDIDATE

    promoted, decision = ledger.promote(
        claim.claim_id, target=Visibility.GRAPH, attempt_succeeded=True
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_VALIDATION_FAILED,)
    assert promoted.status is ClaimStatus.CANDIDATE
    assert ledger.get_claim(claim.claim_id).status is ClaimStatus.CANDIDATE


def test_validation_history_is_bounded(state):
    ledger = _ledger(state)
    claim = ledger.record_claim(statement="bounded history", scope=_scope())
    for index in range(25):
        claim, _ = ledger.validate_claim(
            claim.claim_id,
            result=ValidationResult.INCONCLUSIVE,
            strategy=f"strategy-{index}",
            validator="worker-a",
        )
    stored = ledger.get_claim(claim.claim_id)
    assert len(stored.validations) == 20
    assert stored.validations[-1].strategy == "strategy-24"


# ---------------------------------------------------------------------------
# Promotion: denial paths never mutate
# ---------------------------------------------------------------------------


def test_promote_denied_leaves_claim_and_version_untouched(state):
    ledger = _ledger(state)
    claim = ledger.record_claim(statement="unvalidated claim", scope=_scope())
    before = ledger.get_claim(claim.claim_id)

    promoted, decision = ledger.promote(claim.claim_id, target=Visibility.GRAPH)
    assert decision.denied is True
    assert decision.reasons == (REASON_ATTEMPT_NOT_SUCCESSFUL, REASON_VALIDATION_MISSING)
    assert promoted == before
    assert ledger.get_claim(claim.claim_id) == before
    versioned = state.versioned_get(CLAIM_NAMESPACE, claim.claim_id)
    assert versioned is not None
    assert versioned.version == 1


def test_promote_denied_for_terminal_claim(state):
    ledger = _ledger(state)
    claim = ledger.record_claim(statement="retracted claim", scope=_scope())
    retracted = ledger.retract(claim.claim_id, reason="wrong")
    assert retracted.status is ClaimStatus.RETRACTED

    promoted, decision = ledger.promote(
        claim.claim_id, target=Visibility.GRAPH, attempt_succeeded=True
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_CLAIM_TERMINAL,)
    assert promoted.status is ClaimStatus.RETRACTED
    assert state.versioned_get(CLAIM_NAMESPACE, claim.claim_id).version == 2


def test_promote_denied_when_rollout_stage_disables_graph(state):
    ledger = _ledger(state, KnowledgeStage.SHADOW)
    claim = _validated_claim(ledger, statement="stage gated graph")
    version_before = state.versioned_get(CLAIM_NAMESPACE, claim.claim_id).version

    promoted, decision = ledger.promote(
        claim.claim_id, target=Visibility.GRAPH, attempt_succeeded=True
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_STAGE_DISABLED,)
    assert promoted.visibility is Visibility.ATTEMPT
    assert state.versioned_get(CLAIM_NAMESPACE, claim.claim_id).version == version_before


def test_promote_denied_when_rollout_stage_disables_repository(state):
    ledger = _ledger(state, KnowledgeStage.GRAPH)
    claim = _validated_claim(ledger, statement="stage gated repo", visibility=Visibility.GRAPH)
    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.REPOSITORY,
        attempt_succeeded=True,
        integration=_integration(),
        push_status="pushed",
    )
    assert decision.denied is True
    assert decision.reasons == (REASON_STAGE_DISABLED,)
    assert promoted.visibility is Visibility.GRAPH


def test_promote_denied_on_revision_and_graph_scope_mismatch(state):
    ledger = _ledger(state)
    validated = _validated_claim(ledger, statement="revision bound", revision="abcdef1")
    promoted, decision = ledger.promote(
        validated.claim_id,
        target=Visibility.GRAPH,
        attempt_succeeded=True,
        target_revision="1234567",
    )
    assert decision.reasons == (REASON_REVISION_MISMATCH,)
    assert promoted.visibility is Visibility.ATTEMPT

    scoped = _validated_claim(ledger, statement="no graph scope", scope=_scope(graph_id=""))
    promoted, decision = ledger.promote(
        scoped.claim_id,
        target=Visibility.GRAPH,
        attempt_succeeded=True,
    )
    assert decision.reasons == (REASON_GRAPH_SCOPE_MISSING,)
    assert promoted.visibility is Visibility.ATTEMPT


# ---------------------------------------------------------------------------
# Promotion: allowed paths mutate exactly once and persist
# ---------------------------------------------------------------------------


def test_graph_promotion_persists_visibility_and_status(state):
    ledger = _ledger(state)
    claim = _validated_claim(ledger, statement="promote me")
    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.GRAPH,
        attempt_succeeded=True,
    )
    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.visibility is Visibility.GRAPH
    assert promoted.status is ClaimStatus.PROMOTED
    assert promoted.visibility is Visibility.GRAPH
    assert promoted.promoted_visibility is Visibility.GRAPH
    assert promoted.record_version == claim.record_version + 1

    stored = ledger.get_claim(claim.claim_id)
    assert stored == promoted
    versioned = state.versioned_get(CLAIM_NAMESPACE, claim.claim_id)
    assert versioned is not None
    assert versioned.version == promoted.record_version


def test_repository_promotion_requires_integration_and_known_push(state):
    ledger = _ledger(state)
    claim = _validated_claim(ledger, statement="repo promote", visibility=Visibility.GRAPH)

    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.REPOSITORY,
        attempt_succeeded=True,
    )
    assert decision.reasons == (REASON_INTEGRATION_MISSING,)
    assert promoted.visibility is Visibility.GRAPH

    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.REPOSITORY,
        attempt_succeeded=True,
        integration=_integration(),
        push_status="unknown",
    )
    assert decision.reasons == (REASON_PUSH_UNKNOWN,)
    assert promoted.visibility is Visibility.GRAPH

    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.REPOSITORY,
        attempt_succeeded=True,
        integration=_integration(),
        push_status="pushed",
        target_revision="abc1234",
    )
    assert decision.allowed is True
    assert promoted.status is ClaimStatus.PROMOTED
    assert promoted.visibility is Visibility.REPOSITORY
    assert ledger.get_claim(claim.claim_id).visibility is Visibility.REPOSITORY


def test_promotion_through_ledger_can_lock_in_same_write(state):
    ledger = _ledger(state)
    claim = _validated_claim(ledger, statement="locked promote")
    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.GRAPH,
        attempt_succeeded=True,
        human_locked=True,
    )
    assert decision.allowed is True
    assert promoted.human_locked is True
    assert state.versioned_get(CLAIM_NAMESPACE, claim.claim_id).version == promoted.record_version


def test_human_lock_blocks_supersede_but_not_retract_or_promotion(state):
    ledger = _ledger(state)
    claim = _validated_claim(ledger, statement="human locked claim")
    locked = ledger.lock(claim.claim_id)
    assert locked.human_locked is True

    with pytest.raises(ValueError):
        ledger.supersede(claim.claim_id, new_claim_id="replacement")

    promoted, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.GRAPH,
        attempt_succeeded=True,
    )
    assert decision.allowed is True
    assert promoted.human_locked is True

    retracted = ledger.retract(claim.claim_id, reason="human decision")
    assert retracted.status is ClaimStatus.RETRACTED
    assert retracted.retract_reason == "human decision"


# ---------------------------------------------------------------------------
# Lifecycle: supersede / retract / lock
# ---------------------------------------------------------------------------


def test_supersede_and_retract_are_terminal_idempotent_and_never_delete(state):
    ledger = _ledger(state)
    claim = ledger.record_claim(statement="supersede me", scope=_scope())
    superseded = ledger.supersede(claim.claim_id, new_claim_id="claim-2", reason="replaced")
    assert superseded.status is ClaimStatus.SUPERSEDED
    assert superseded.superseded_by == "claim-2"
    assert superseded.retract_reason == "replaced"
    assert superseded.is_terminal() is True

    again = ledger.supersede(claim.claim_id)
    assert again == superseded
    retracted_after = ledger.retract(claim.claim_id, reason="ignored")
    assert retracted_after == superseded
    assert ledger.get_claim(claim.claim_id) == superseded

    other = ledger.record_claim(statement="retract me", scope=_scope())
    retracted = ledger.retract(other.claim_id, reason="wrong")
    assert retracted.status is ClaimStatus.RETRACTED
    assert retracted.retract_reason == "wrong"
    assert ledger.get_claim(other.claim_id).status is ClaimStatus.RETRACTED


def test_lock_toggle_is_idempotent_and_unknown_claims_raise(state):
    ledger = _ledger(state)
    claim = ledger.record_claim(statement="lock me", scope=_scope())
    locked = ledger.lock(claim.claim_id)
    assert locked.human_locked is True
    assert locked.record_version == 2
    assert ledger.lock(claim.claim_id).record_version == 2
    unlocked = ledger.lock(claim.claim_id, locked=False)
    assert unlocked.human_locked is False

    for operation in (
        lambda: ledger.promote("missing", target=Visibility.GRAPH),
        lambda: ledger.supersede("missing"),
        lambda: ledger.retract("missing"),
        lambda: ledger.lock("missing"),
    ):
        with pytest.raises(ValueError):
            operation()


# ---------------------------------------------------------------------------
# Read visibility
# ---------------------------------------------------------------------------


def test_visible_applies_gate_scope_revision_and_sorting(state):
    ledger = _ledger(state)
    attempt_claim = _validated_claim(ledger, statement="attempt scoped")
    graph_claim = _validated_claim(ledger, statement="graph scoped", visibility=Visibility.GRAPH)
    repo_claim = _validated_claim(
        ledger,
        statement="repo scoped",
        visibility=Visibility.REPOSITORY,
        binding=RevisionBinding(revision="abc1234"),
    )

    graph_reader = ledger.visible(
        scope=_scope(),
        reader_visibility=Visibility.GRAPH,
        same_chain=True,
    )
    assert [claim.claim_id for claim in graph_reader] == [
        graph_claim.claim_id,
        attempt_claim.claim_id,
    ]

    repo_reader = ledger.visible(
        scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        reader_revision="abc1234",
        same_chain=True,
    )
    assert [claim.claim_id for claim in repo_reader] == [
        repo_claim.claim_id,
        graph_claim.claim_id,
        attempt_claim.claim_id,
    ]

    limited = ledger.visible(
        scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        reader_revision="abc1234",
        same_chain=True,
        min_visibility=Visibility.GRAPH,
        limit=1,
    )
    assert [claim.claim_id for claim in limited] == [repo_claim.claim_id]

    mismatched_revision = ledger.visible(
        scope=_scope(),
        reader_visibility=Visibility.REPOSITORY,
        reader_revision="9999999",
        same_chain=True,
    )
    assert repo_claim.claim_id not in [claim.claim_id for claim in mismatched_revision]
    assert graph_claim.claim_id in [claim.claim_id for claim in mismatched_revision]

    attempt_hidden = ledger.visible(
        scope=_scope(), reader_visibility=Visibility.ATTEMPT, same_chain=False
    )
    assert attempt_hidden == []
    assert ledger.visible(
        scope=_scope(),
        reader_visibility=Visibility.GRAPH,
        reader_scope=_scope(tenant_id="tenant-b"),
        same_chain=True,
    ) == []

    other_branch = _validated_claim(
        ledger,
        statement="claim on another branch",
        visibility=Visibility.GRAPH,
        scope=_scope(branch="dev"),
    )
    other_repo = _validated_claim(
        ledger,
        statement="claim in another repository",
        visibility=Visibility.GRAPH,
        scope=_scope(repo_id="repo-2"),
    )
    isolated = ledger.visible(scope=_scope(), reader_visibility=Visibility.GRAPH, same_chain=True)
    isolated_ids = [claim.claim_id for claim in isolated]
    assert other_branch.claim_id not in isolated_ids
    assert other_repo.claim_id not in isolated_ids
    assert graph_claim.claim_id in isolated_ids


def test_claim_visible_only_after_validation(state):
    ledger = _ledger(state)
    candidate = ledger.record_claim(statement="candidate not visible", scope=_scope())
    assert ledger.visible(
        scope=_scope(), reader_visibility=Visibility.ATTEMPT, same_chain=True
    ) == []
    validated = _validated_claim(ledger, statement="validated visible")
    visible = ledger.visible(scope=_scope(), reader_visibility=Visibility.ATTEMPT, same_chain=True)
    ids = [claim.claim_id for claim in visible]
    assert validated.claim_id in ids
    assert candidate.claim_id not in ids


# ---------------------------------------------------------------------------
# Serialisation, logging and environment configuration
# ---------------------------------------------------------------------------


def test_records_round_trip_through_json(state):
    ledger = _ledger(state)
    evidence = ledger.record_evidence(
        attempt=_attempt(),
        kind=EvidenceKind.FAILURE_SIGNATURE,
        payload={"signature": "KeyError", "nested": {"line": 12}},
        summary="failure",
        revision=RevisionBinding(revision="abc1234"),
    )
    assert EvidenceRecord.from_dict(json.loads(json.dumps(evidence.to_dict()))) == evidence

    claim = _validated_claim(ledger, statement="round trip")
    restored = ClaimRecord.from_dict(json.loads(json.dumps(claim.to_dict())))
    assert restored == claim
    assert len(restored.validations) == 1
    validation = restored.validations[0]
    assert ValidationRecord.from_dict(json.loads(json.dumps(validation.to_dict()))) == validation


def test_log_summaries_never_include_claim_text_or_evidence_payload(state):
    ledger = _ledger(state)
    evidence = ledger.record_evidence(
        attempt=_attempt(),
        kind=EvidenceKind.PATCH_OBSERVED,
        payload={"secret_payload": "do-not-log"},
    )
    evidence_summary = evidence.to_summary()
    assert "payload" not in evidence_summary
    assert "do-not-log" not in json.dumps(evidence_summary)

    claim = ledger.record_claim(statement="do-not-log statement", scope=_scope())
    claim_summary = claim.to_summary()
    assert "statement" not in claim_summary
    assert "do-not-log" not in json.dumps(claim_summary)


def test_ledger_resolves_stage_from_environment(state, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    assert EvidenceLedger(state).stage is KnowledgeStage.SHADOW
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "")
    assert EvidenceLedger(state).stage is KnowledgeStage.DISABLED
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "canonical")
    assert EvidenceLedger(state).stage is KnowledgeStage.CANONICAL
    assert EvidenceLedger(state, stage=KnowledgeStage.RECOVERY).stage is KnowledgeStage.RECOVERY
