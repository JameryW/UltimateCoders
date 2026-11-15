"""Worker wiring for the attempt diff-chain and the recovery view.

The knowledge layer only pays off if the worker captures what each attempt
changed *while it still can* and then reads those facts back on the retry:

- capture happens after the sandbox returns but before ``release`` (once the
  worktree is merged the candidate diff is gone), and
- a retry recompiles its context with the prior attempt's committed round, so
  the next agent sees the patch/failure facts instead of a free-text error.

Both behaviors are rollout-gated (``UC_KNOWLEDGE_STAGE``) and best-effort:
the default stage is DISABLED, where execution stays byte-identical and
nothing is written, and a ledger failure must never fail the subtask it
describes.

Covers:
- Stage DISABLED: no capture call, no ledger round.
- Stage SHADOW: one round is recorded, ordered before release, with the
  normalized change vocabulary and an honest ``test_verdict`` (a sandbox that
  reports no review/oracle/test verdict leaves the round unverified).
- Stage SHADOW with structured verdict carriers: review/oracle/test verdicts
  become their own evidence rows and the round label names them.
- Stage RECOVERY: attempt 0's round feeds attempt 1's prompt through the
  recovery view; the retry event carries the log-safe summary.
- Stage GRAPH: a delivered attempt becomes a claim the gate promotes; a failed
  attempt's claim is validated at most, never shared.
- Capture failure is swallowed: execution still succeeds.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from ultimate_coders.agent.types import Subtask, SubtaskResult, SubtaskReview
from ultimate_coders.agent.worker import Worker
from ultimate_coders.agent.workspace import WorkspaceHandle
from ultimate_coders.knowledge.attempt_chain import AttemptChainLedger
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.model import (
    AttemptRef,
    EvidenceKind,
    KnowledgeScope,
    RevisionBinding,
    Visibility,
)
from ultimate_coders.knowledge.rollout import KnowledgeStage
from ultimate_coders.runtime_state import RuntimeState

SNAPSHOT = {
    "baseline": "base-sha",
    "head": "head-sha",
    "files": [
        {
            "path": "src/app.py",
            "change_type": "modified",
            "diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,2 @@\n+print('hi')\n",
        }
    ],
    "file_count": 1,
    "truncated": False,
}


class _FakeWorkspaceManager:
    """Minimal workspace manager: one handle, a snapshot, call ordering."""

    def __init__(self, worktree: str, order: list[str]) -> None:
        self._worktree = worktree
        self._order = order
        self.capture_calls = 0
        self.release_calls = 0
        self.acquire_calls = 0
        self.capture_error: Exception | None = None

    def bind_runtime_state(self, state: Any) -> None:  # pragma: no cover - no worktree
        return None

    async def acquire(self, subtask_id: str, **kwargs: Any) -> WorkspaceHandle:
        self.acquire_calls += 1
        return WorkspaceHandle(
            workspace_id="ws-1",
            branch_name=f"uc/subtask/{subtask_id}",
            worktree_path=self._worktree,
            subtask_id=subtask_id,
            status="active",
        )

    async def capture_diff(self, handle: WorkspaceHandle, **kwargs: Any) -> dict[str, Any]:
        self.capture_calls += 1
        self._order.append("capture")
        if self.capture_error is not None:
            raise self.capture_error
        return dict(SNAPSHOT)

    async def release(self, handle: WorkspaceHandle, merge: bool = True) -> dict[str, Any]:
        self.release_calls += 1
        self._order.append("release")
        return {"status": "merged", "commit_sha": "merged-sha"}

    async def quarantine(self, handle: WorkspaceHandle) -> None:  # pragma: no cover
        self._order.append("quarantine")


class _EvidenceWorker(Worker):
    RETRY_DELAYS = [0.0, 0.0]


def _make_worker(
    tmp_path,
    *,
    order: list[str],
    results: list[SubtaskResult],
    events: list[tuple[str, dict[str, Any]]] | None = None,
    captured: list[str] | None = None,
) -> tuple[_EvidenceWorker, _FakeWorkspaceManager]:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    manager = _FakeWorkspaceManager(str(worktree), order)

    w = _EvidenceWorker(worker_id="w-test", engine=None)
    w._workspace_manager = manager  # type: ignore[assignment]

    calls = iter(results)

    async def _fake_execute_in_sandbox(subtask, context_block, workspace_handle):
        if captured is not None:
            captured.append(context_block)
        return next(calls)

    w._execute_in_sandbox = _fake_execute_in_sandbox  # type: ignore[assignment]

    async def _capture_event(event_type: str, **kw: Any) -> None:
        if events is not None:
            events.append((event_type, kw))

    w._publish_event = _capture_event  # type: ignore[assignment]
    return w, manager


def _subtask(max_retries: int) -> Subtask:
    return Subtask(
        id="s1",
        parent_id="t1",
        description="fix the bug",
        file_constraints=["src/app.py"],
        depends_on=["n-0"],
    )


def _state(tmp_path) -> RuntimeState:
    return RuntimeState(tmp_path / "state.sqlite3")


def _rounds(tmp_path) -> list:
    return AttemptChainLedger(_state(tmp_path)).rounds(task_id="t1", subtask_id="s1", limit=10)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UC_HY_MEMORY_AUTO_CONTEXT", "")
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.delenv("UC_KNOWLEDGE_STAGE", raising=False)


def _ok(
    *,
    review: SubtaskReview | None = None,
    domain_result: dict[str, Any] | None = None,
) -> SubtaskResult:
    return SubtaskResult(
        subtask_id="s1",
        worker_id="w-test",
        summary="done",
        success=True,
        review=review,
        domain_result=domain_result,
    )


def _failed() -> SubtaskResult:
    return SubtaskResult(
        subtask_id="s1",
        worker_id="w-test",
        summary="attempt failed",
        success=False,
        retryable=True,
        error="boom",
    )


async def test_disabled_stage_captures_nothing(tmp_path, monkeypatch):
    order: list[str] = []
    w, manager = _make_worker(tmp_path, order=order, results=[_ok()])
    await w.execute_subtask(_subtask(1))

    assert manager.capture_calls == 0
    assert _rounds(tmp_path) == []


async def test_shadow_stage_records_round_before_release(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    order: list[str] = []
    w, manager = _make_worker(tmp_path, order=order, results=[_ok()])
    await w.execute_subtask(_subtask(1))

    assert manager.capture_calls == 1
    # The candidate diff must be captured while the worktree is still held.
    assert order.index("capture") < order.index("release")

    rounds = _rounds(tmp_path)
    assert len(rounds) == 1
    recorded = rounds[0]
    assert recorded.attempt.task_id == "t1"
    assert recorded.attempt.subtask_id == "s1"
    assert recorded.attempt.dispatch_attempt == 0
    assert recorded.attempt.attempt == 0
    assert recorded.attempt.worker_id == "w-test"
    assert recorded.outcome == "completed"
    assert recorded.base_revision == "base-sha"
    assert recorded.head_revision == "head-sha"
    assert recorded.changed_paths == ("src/app.py",)
    assert recorded.patches[0].change_type == "modified"
    # The sandbox returns no oracle/test evidence; a pass/fail verdict here
    # would be fabricated (principle A: facts and interpretations separate).
    assert recorded.test_verdict == "unknown"


async def test_capture_failure_does_not_fail_the_subtask(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    order: list[str] = []
    w, manager = _make_worker(tmp_path, order=order, results=[_ok()])
    manager.capture_error = RuntimeError("ledger unavailable")

    result = await w.execute_subtask(_subtask(1))

    assert result.success is True
    assert _rounds(tmp_path) == []


async def test_recovery_stage_injects_prior_round_into_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "recovery")
    order: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []
    captured: list[str] = []

    w, _manager = _make_worker(
        tmp_path, order=order, results=[_failed(), _ok()], events=events
    )

    async def _fake_execute_in_sandbox(subtask, context_block, workspace_handle):
        captured.append(context_block)
        return _failed() if len(captured) == 1 else _ok()

    w._execute_in_sandbox = _fake_execute_in_sandbox  # type: ignore[assignment]

    result = await w.execute_subtask(_subtask(2))

    assert result.success is True
    assert len(captured) == 2
    # Attempt 0 runs with the base context only.
    assert "## Prior attempt evidence" not in captured[0]
    # Attempt 1 sees what attempt 0 changed and why it failed.
    recovery_text = captured[1]
    assert "## Prior attempt evidence" in recovery_text
    assert "src/app.py" in recovery_text
    assert "boom" in recovery_text

    retry_events = [kw for event_type, kw in events if event_type == "subtask_retry"]
    assert len(retry_events) == 1
    recovery = retry_events[0]["data"]["recovery"]
    assert recovery["rounds_shown"] == 1
    # The event stays log-safe: counts, never prompt or diff content.
    assert "boom" not in json.dumps(recovery)
    assert "print" not in json.dumps(recovery)


def _evidence(tmp_path, attempt: AttemptRef) -> list:
    ledger = EvidenceLedger(_state(tmp_path))
    return ledger.evidence_for(attempt)


async def test_attempt_outcome_and_merge_receipt_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    order: list[str] = []
    w, _manager = _make_worker(tmp_path, order=order, results=[_ok()])
    await w.execute_subtask(_subtask(1))

    ref = AttemptRef(
        task_id="t1",
        subtask_id="s1",
        dispatch_attempt=0,
        attempt=0,
        worker_id="w-test",
    )
    records = _evidence(tmp_path, ref)
    kinds = {record.kind for record in records}
    # The conclusion and the integration receipt are distinct facts: the
    # attempt outcome is worker-reported, the merge receipt is what the target
    # accepted. A later promotion gates on both separately.
    assert EvidenceKind.ATTEMPT_OUTCOME in kinds
    assert EvidenceKind.MERGE_RECEIPT in kinds
    # Every record stays attempt-scoped: capture never promotes on its own.
    assert {record.visibility.value for record in records} == {"attempt"}
    receipt = next(r for r in records if r.kind is EvidenceKind.MERGE_RECEIPT)
    assert receipt.payload["status"] == "merged"
    assert receipt.payload["commit_sha"] == "merged-sha"


async def test_failed_attempt_records_a_failure_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    order: list[str] = []
    w, _manager = _make_worker(tmp_path, order=order, results=[_failed()])
    result = await w.execute_subtask(_subtask(1))
    assert result.success is False

    ref = AttemptRef(
        task_id="t1",
        subtask_id="s1",
        dispatch_attempt=0,
        attempt=0,
        worker_id="w-test",
    )
    records = _evidence(tmp_path, ref)
    kinds = {record.kind for record in records}
    assert EvidenceKind.ATTEMPT_OUTCOME in kinds
    assert EvidenceKind.FAILURE_SIGNATURE in kinds
    signature = next(r for r in records if r.kind is EvidenceKind.FAILURE_SIGNATURE)
    assert "boom" in signature.payload["signature"]


def _claims(tmp_path) -> list[dict[str, Any]]:
    return _state(tmp_path).records("knowledge_claims")


def _ref() -> AttemptRef:
    return AttemptRef(
        task_id="t1",
        subtask_id="s1",
        dispatch_attempt=0,
        attempt=0,
        worker_id="w-test",
        graph_id="t1",
    )


async def test_reported_verdicts_become_evidence_and_a_round_label(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    order: list[str] = []
    result = _ok(
        review=SubtaskReview(approved=True, issues=["add a regression test"]),
        domain_result={
            "oracle": {"accepted": True, "reasons": ["faster"]},
            "tests": {"passed": True, "summary": "12 passed"},
        },
    )
    w, _manager = _make_worker(tmp_path, order=order, results=[result])
    await w.execute_subtask(_subtask(1))

    # The round states what was verified, so a recovery view can show it.
    assert _rounds(tmp_path)[0].test_verdict == "review=approved oracle=accepted test=passed"

    records = _evidence(tmp_path, _ref())
    kinds = {record.kind for record in records}
    assert {
        EvidenceKind.REVIEW_VERDICT,
        EvidenceKind.ORACLE_VERDICT,
        EvidenceKind.TEST_VERDICT,
    } <= kinds

    review = next(r for r in records if r.kind is EvidenceKind.REVIEW_VERDICT)
    assert review.payload["verdict"] == "approved"
    assert review.payload["issues"] == ["add a regression test"]
    oracle = next(r for r in records if r.kind is EvidenceKind.ORACLE_VERDICT)
    assert oracle.payload["verdict"] == "accepted"
    tests = next(r for r in records if r.kind is EvidenceKind.TEST_VERDICT)
    assert tests.payload["verdict"] == "passed"


async def test_shadow_records_a_delivery_claim_without_promoting(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    order: list[str] = []
    w, _manager = _make_worker(tmp_path, order=order, results=[_ok()])
    await w.execute_subtask(_subtask(1))

    claims = _claims(tmp_path)
    assert len(claims) == 1
    assert claims[0]["visibility"] == "attempt"
    assert claims[0]["status"] == "candidate"
    assert "delivered" in claims[0]["statement"]


async def test_graph_stage_promotes_a_delivery_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "graph")
    order: list[str] = []
    w, _manager = _make_worker(tmp_path, order=order, results=[_ok()])
    await w.execute_subtask(_subtask(1))

    claims = _claims(tmp_path)
    assert len(claims) == 1
    # The merge receipt is the acceptance evidence; the gate, not the worker,
    # widened the audience.
    assert claims[0]["status"] == "promoted"
    assert claims[0]["visibility"] == "graph"
    validations = claims[0]["validations"]
    assert validations and validations[-1]["result"] == "pass"
    assert validations[-1]["strategy"] == "merge-receipt"


async def test_failed_attempt_never_promotes_its_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "graph")
    order: list[str] = []
    w, _manager = _make_worker(tmp_path, order=order, results=[_failed()])
    result = await w.execute_subtask(_subtask(1))
    assert result.success is False

    claims = _claims(tmp_path)
    assert len(claims) == 2  # failure claim + delivery claim
    assert {claim["visibility"] for claim in claims} == {"attempt"}
    assert all(claim["status"] != "promoted" for claim in claims)
    failure = next(claim for claim in claims if "failed:" in claim["statement"])
    assert "boom" in failure["statement"]


def _promoted_graph_claim(tmp_path, statement: str = "prior attempt delivered to main"):
    """Seed a claim the gate already promoted inside task graph ``t1``."""

    ledger = EvidenceLedger(_state(tmp_path), stage=KnowledgeStage.GRAPH)
    attempt = AttemptRef(
        task_id="t1",
        subtask_id="seed",
        dispatch_attempt=0,
        attempt=0,
        worker_id="w-seed",
        graph_id="t1",
    )
    evidence = ledger.record_evidence(
        attempt=attempt,
        kind=EvidenceKind.ATTEMPT_OUTCOME,
        payload={"success": True},
    )
    claim = ledger.record_claim(
        statement=statement,
        # Production delivery claims use the attempt's graph scope without a
        # pinned branch (``attempt_scope(ref)``); the reader uses the same.
        scope=KnowledgeScope(graph_id="t1"),
        attempt=attempt,
        evidence_ids=(evidence.evidence_id,),
        binding=RevisionBinding(branch="main", revision="a" * 40),
        confidence=0.8,
    )
    ledger.validate_claim(claim.claim_id, result="pass", revision="a" * 40)
    _claim, decision = ledger.promote(
        claim.claim_id,
        target=Visibility.GRAPH,
        attempt_succeeded=True,
        target_revision="a" * 40,
    )
    assert decision.allowed is True


async def test_graph_stage_injects_promoted_graph_knowledge(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "graph")
    _promoted_graph_claim(tmp_path)
    order: list[str] = []
    captured: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []
    w, _manager = _make_worker(
        tmp_path, order=order, results=[_ok()], events=events, captured=captured
    )

    await w.execute_subtask(_subtask(1))

    assert len(captured) == 1
    assert "prior attempt delivered to main" in captured[0]
    assert "Claim (graph)" in captured[0]
    started = [data for event, data in events if event == "subtask_started"]
    assert started and started[0]["data"]["knowledge"]["claims_used"] == 1
    assert started[0]["data"]["knowledge"]["claims_visible"] == 1


async def test_shadow_stage_never_reads_graph_knowledge(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_STAGE", "shadow")
    _promoted_graph_claim(tmp_path)
    order: list[str] = []
    captured: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []
    w, _manager = _make_worker(
        tmp_path, order=order, results=[_ok()], events=events, captured=captured
    )

    await w.execute_subtask(_subtask(1))

    assert "prior attempt delivered to main" not in captured[0]
    started = [data for event, data in events if event == "subtask_started"]
    assert started and "knowledge" not in started[0]["data"]


