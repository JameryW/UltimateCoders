"""Integration receipts written by the merge arbiter.

The worker cannot see whether its branch actually landed on the target ref; the
arbiter can. These tests pin that second observation point:

- only a merged-and-published result produces a receipt (a conflict or a failed
  push must not claim anything landed),
- receipts are target-level evidence (repository visibility) and never a
  substitute for validation,
- repository promotion happens only at stage ``canonical`` with a graph scope,
  a known push verdict and a matching revision, and a denial changes nothing,
- every write is rollout-gated (stage A writes nothing) and best-effort (a
  knowledge failure never changes the arbitration result).
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.merge_arbiter import MergeArbiter
from ultimate_coders.knowledge.gate import REASON_PUSH_UNKNOWN
from ultimate_coders.knowledge.integration import (
    INTEGRATION_STRATEGY,
    integration_attempt,
    integration_binding,
    record_integration,
)
from ultimate_coders.knowledge.model import ClaimStatus, EvidenceKind, KnowledgeScope, Visibility
from ultimate_coders.runtime_state import RuntimeState

SHA = "e" * 40


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("UC_KNOWLEDGE_STAGE", raising=False)


@pytest.fixture()
def state(tmp_path):
    return RuntimeState(tmp_path / "integration-state.sqlite3", database_url="")


def _evidence(state) -> list[dict]:
    return state.records("knowledge_evidence")


def _claims(state) -> list[dict]:
    return state.records("knowledge_claims")


def _pages(state) -> list[dict]:
    return state.records("knowledge_pages")


def _graph_scope() -> KnowledgeScope:
    return KnowledgeScope(graph_id="g1", branch="main")


class TestIntegrationIdentity:
    def test_branch_identity_uses_the_last_path_segment(self):
        attempt = integration_attempt("uc/subtask/s1")

        assert attempt.subtask_id == "s1"
        assert attempt.worker_id == "merge-arbiter"
        assert attempt.graph_id == ""

    def test_binding_names_the_target_ref_and_landed_sha(self):
        binding = integration_binding(
            base_branch="main", remote_name="origin", integration_sha=SHA
        )

        assert binding.branch == "main"
        assert binding.revision == SHA
        assert binding.integration_ref == "origin/main"
        assert binding.integration_sha == SHA
        assert binding.has_integration is True


class TestRecordIntegration:
    def test_stage_disabled_writes_nothing(self, state):
        summary = record_integration(
            state,
            branches=["uc/subtask/s1"],
            integration_sha=SHA,
            push_status="pushed",
            stage=None,
        )

        assert summary["skipped"] is True
        assert summary["evidence_records"] == 0
        assert _evidence(state) == []
        assert _claims(state) == []

    def test_shadow_records_receipts_as_repository_evidence(self, state):
        summary = record_integration(
            state,
            branches=["uc/subtask/s1", "uc/subtask/s2"],
            integration_sha=SHA,
            push_status="pushed",
            stage="shadow",  # type: ignore[arg-type]
        )

        assert summary["skipped"] is False
        assert summary["branches"] == 2
        assert summary["evidence_records"] == 4
        assert summary["claims"] == 0
        # Pages are derived from promoted claims, so a capture-only stage
        # derives nothing (the projection is canonical-stage work).
        assert summary["pages"] == 0
        assert _pages(state) == []
        records = _evidence(state)
        assert {record["kind"] for record in records} == {
            EvidenceKind.MERGE_RECEIPT.value,
            EvidenceKind.PUSH_VERDICT.value,
        }
        # A target-level fact outranks one graph's attempt scope.
        assert {record["visibility"] for record in records} == {Visibility.REPOSITORY.value}
        receipts = [r for r in records if r["kind"] == EvidenceKind.MERGE_RECEIPT.value]
        assert {r["payload"]["status"] for r in receipts} == {"merged"}
        assert {r["payload"]["commit_sha"] for r in receipts} == {SHA}
        assert {r["revision"]["integration_ref"] for r in receipts} == {"origin/main"}

    def test_not_merged_payload_is_honest_and_promotes_nothing(self, state):
        summary = record_integration(
            state,
            branches=["uc/subtask/s1"],
            integration_sha=SHA,
            merged=False,
            scope=_graph_scope(),
            stage="canonical",  # type: ignore[arg-type]
        )

        assert summary["claims"] == 0
        receipts = [
            r for r in _evidence(state) if r["kind"] == EvidenceKind.MERGE_RECEIPT.value
        ]
        assert receipts[0]["payload"]["status"] == "not-merged"

    def test_canonical_without_a_graph_scope_records_receipts_only(self, state):
        summary = record_integration(
            state,
            branches=["uc/subtask/s1"],
            integration_sha=SHA,
            push_status="pushed",
            stage="canonical",  # type: ignore[arg-type]
        )

        assert summary["evidence_records"] == 2
        assert summary["claims"] == 0
        assert _claims(state) == []

    def test_canonical_promotes_with_a_receipt_validation(self, state):
        summary = record_integration(
            state,
            branches=["uc/subtask/s1"],
            integration_sha=SHA,
            push_status="pushed",
            scope=_graph_scope(),
            stage="canonical",  # type: ignore[arg-type]
        )

        assert summary["claims"] == 1
        assert summary["promoted"] == 1
        claim = _claims(state)[0]
        assert claim["status"] == ClaimStatus.PROMOTED.value
        assert claim["visibility"] == Visibility.REPOSITORY.value
        assert claim["binding"]["integration_sha"] == SHA
        validation = claim["validations"][-1]
        assert validation["result"] == "pass"
        assert validation["strategy"] == INTEGRATION_STRATEGY
        assert validation["validator"] == "merge-arbiter"

    def test_canonical_promotion_rebuilds_the_repository_page(self, state):
        summary = record_integration(
            state,
            branches=["uc/subtask/s1"],
            integration_sha=SHA,
            push_status="pushed",
            scope=_graph_scope(),
            stage="canonical",  # type: ignore[arg-type]
        )

        assert summary["promoted"] == 1
        assert summary["pages"] == 1
        page = _pages(state)[0]
        claim = _claims(state)[0]
        # One canonical page per repository/branch, not per graph.
        assert page["scope"]["graph_id"] == ""
        assert page["scope"]["branch"] == "main"
        assert page["lifecycle"] == "active"
        # The page projects the authoritative claims and pins the landed
        # revision/integration from the receipt rather than inventing one.
        assert [ref["claim_id"] for ref in page["claim_refs"]] == [claim["claim_id"]]
        assert page["binding"]["integration_sha"] == SHA
        assert page["binding"]["integration_ref"] == "origin/main"
        assert page["binding"]["revision"] == SHA
        # Bodies stay counts-only — no claim statement text is copied in.
        assert "claims: 1" in page["body"]
        assert "landed on" not in page["body"]

    def test_rebuild_is_idempotent_across_repeated_batches(self, state):
        for _ in range(2):
            record_integration(
                state,
                branches=["uc/subtask/s1"],
                integration_sha=SHA,
                push_status="pushed",
                scope=_graph_scope(),
                stage="canonical",  # type: ignore[arg-type]
            )

        pages = _pages(state)
        assert len(pages) == 1
        assert len(pages[0]["claim_refs"]) == 1

    def test_page_projection_failure_is_counted_not_raised(self, state, monkeypatch):
        class _BrokenPages:
            def __init__(self, _state):
                pass

            def rebuild(self, **_kwargs):
                raise RuntimeError("page projection broke")

        monkeypatch.setattr(
            "ultimate_coders.knowledge.integration.KnowledgePages", _BrokenPages
        )
        summary = record_integration(
            state,
            branches=["uc/subtask/s1"],
            integration_sha=SHA,
            push_status="pushed",
            scope=_graph_scope(),
            stage="canonical",  # type: ignore[arg-type]
        )

        # The authoritative claim still promoted; only the projection failed.
        assert summary["promoted"] == 1
        assert summary["pages"] == 0
        assert summary["errors"] == 1
        assert _claims(state)[0]["status"] == ClaimStatus.PROMOTED.value

    def test_unknown_push_verdict_blocks_promotion(self, state):
        summary = record_integration(
            state,
            branches=["uc/subtask/s1"],
            integration_sha=SHA,
            push_status="UNKNOWN",
            scope=_graph_scope(),
            stage="canonical",  # type: ignore[arg-type]
        )

        assert summary["promoted"] == 0
        assert summary["denied"] == 1
        assert REASON_PUSH_UNKNOWN in summary["reasons"]
        claim = _claims(state)[0]
        assert claim["status"] != ClaimStatus.PROMOTED.value
        assert claim["visibility"] == Visibility.ATTEMPT.value


class TestArbiterWiring:
    def _arbiter(self, tmp_path, **kwargs) -> MergeArbiter:
        return MergeArbiter(
            project_path=str(tmp_path),
            remote_url="git@example.invalid:repo.git",
            base_branch="main",
            **kwargs,
        )

    async def test_merged_result_carries_a_log_safe_knowledge_summary(
        self, tmp_path, state, monkeypatch
    ):
        monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
        arbiter = self._arbiter(tmp_path, knowledge_state=state)

        async def _head() -> str:
            return SHA

        arbiter._head = _head  # type: ignore[assignment]
        result = {
            "status": "merged",
            "merged_branches": ["uc/subtask/s1"],
            "push_status": "pushed",
        }

        await arbiter._record_integration(result)

        assert result["knowledge"]["evidence_records"] == 2
        assert len(_evidence(state)) == 2

    async def test_non_merged_result_is_not_recorded(self, tmp_path, state, monkeypatch):
        monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
        arbiter = self._arbiter(tmp_path, knowledge_state=state)
        result = {"status": "conflict", "merged_branches": [], "push_status": ""}

        await arbiter._record_integration(result)

        assert "knowledge" not in result
        assert _evidence(state) == []

    async def test_no_state_means_no_capture(self, tmp_path):
        arbiter = self._arbiter(tmp_path)
        result = {"status": "merged", "merged_branches": ["uc/subtask/s1"]}

        await arbiter._record_integration(result)

        assert "knowledge" not in result

    async def test_capture_failure_never_changes_the_result(
        self, tmp_path, state, monkeypatch
    ):
        monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
        arbiter = self._arbiter(tmp_path, knowledge_state=state)

        async def _head() -> str:
            raise RuntimeError("git unavailable")

        arbiter._head = _head  # type: ignore[assignment]
        result = {"status": "merged", "merged_branches": ["uc/subtask/s1"]}

        await arbiter._record_integration(result)

        assert result["status"] == "merged"
        assert "knowledge" not in result
        assert _evidence(state) == []

    async def test_stage_disabled_skips_even_with_state(self, tmp_path, state):
        arbiter = self._arbiter(tmp_path, knowledge_state=state)

        async def _head() -> str:
            return SHA

        arbiter._head = _head  # type: ignore[assignment]
        result = {"status": "merged", "merged_branches": ["uc/subtask/s1"]}

        await arbiter._record_integration(result)

        assert result["knowledge"]["skipped"] is True
        assert _evidence(state) == []
