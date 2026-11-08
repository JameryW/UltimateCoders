"""P0 — Decision Record outbox + worker best-effort writes.

One attempt-scoped record joins route, context digest/key, and terminal
verdict without storing prompt content. Persistence failure must never fail
the subtask it describes.
"""

from __future__ import annotations

import hashlib
import itertools
import json

import pytest
from ultimate_coders.agent.context_compiler import (
    ContextItem,
    Tier,
    compile_context,
)
from ultimate_coders.agent.routing import ReasoningBudget, RouteDecision
from ultimate_coders.agent.types import (
    ChangeType,
    FileChange,
    Subtask,
    SubtaskResult,
    SubtaskReview,
)
from ultimate_coders.agent.worker import Worker
from ultimate_coders.context_outbox import ContextManifestOutbox, manifest_key
from ultimate_coders.decision_record import (
    DecisionRecordOutbox,
    context_digest,
    decision_key,
)
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return RuntimeState()


def _summary(budget: int = 100, *, risk: str = "normal") -> dict:
    manifest = compile_context(
        [ContextItem(item_id="n-0", tier=Tier.TRACE, content="x" * 40)],
        budget_tokens=budget,
        risk=risk,
    )
    return manifest.summary()


# ── key + digest identity ────────────────────────────────────────


def test_decision_key_includes_the_attempt():
    assert decision_key("t", "s", 0) == "t:s:0"
    assert decision_key("t", "s", 0) != decision_key("t", "s", 1)


def test_context_digest_is_canonical_sha256():
    summary = _summary()
    expected = hashlib.sha256(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert context_digest(summary) == expected
    # Insertion order must not change the digest.
    shuffled = {k: summary[k] for k in reversed(list(summary))}
    assert context_digest(shuffled) == expected


# ── start / terminal merge ───────────────────────────────────────


def test_record_start_round_trips(state):
    outbox = DecisionRecordOutbox(state)
    key = decision_key("t", "s", 0)
    outbox.record_start(
        key,
        worker_id="w1",
        route={"stage": "implementation", "risk": "medium"},
        context_digest="abc",
        context_key="t:s:0",
    )

    rows = outbox.history("t", "s")
    assert len(rows) == 1
    assert rows[0]["key"] == "t:s:0"
    assert rows[0]["worker_id"] == "w1"
    assert rows[0]["route"]["stage"] == "implementation"
    assert rows[0]["context_digest"] == "abc"
    assert rows[0]["context_key"] == "t:s:0"
    assert rows[0]["recorded_at"] > 0
    assert "terminal" not in rows[0]


def test_record_stores_no_prompt_content(state):
    outbox = DecisionRecordOutbox(state)
    outbox.record_start(
        decision_key("t", "s", 0),
        worker_id="w1",
        context_digest=context_digest(_summary()),
        context_key="t:s:0",
    )
    outbox.record_terminal(
        decision_key("t", "s", 0),
        success=True,
        summary="ok",
    )

    record = outbox.history("t", "s")[0]
    blob = json.dumps(record)
    assert "content" not in record
    assert "prompt" not in blob
    assert set(record) <= {
        "key",
        "recorded_at",
        "worker_id",
        "route",
        "context_digest",
        "context_key",
        "governance",
        "terminal",
        "terminal_at",
    }


def test_identical_start_is_a_no_op(state, monkeypatch):
    outbox = DecisionRecordOutbox(state)
    key = decision_key("t", "s", 0)
    clock = itertools.count(1000.0)
    monkeypatch.setattr(
        "ultimate_coders.decision_record.time.time", lambda: next(clock)
    )
    outbox.record_start(key, worker_id="w1", context_digest="d")
    outbox.record_start(key, worker_id="w1", context_digest="d")

    record = outbox.history("t", "s")[0]
    assert record["recorded_at"] == 1000.0
    assert record["worker_id"] == "w1"


def test_different_start_replaces_and_restamps(state, monkeypatch):
    outbox = DecisionRecordOutbox(state)
    key = decision_key("t", "s", 0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "ultimate_coders.decision_record.time.time", lambda: clock["now"]
    )
    outbox.record_start(key, worker_id="w1", context_digest="d1")
    clock["now"] = 5000.0
    outbox.record_start(key, worker_id="w2", context_digest="d2")

    record = outbox.history("t", "s")[0]
    assert record["worker_id"] == "w2"
    assert record["context_digest"] == "d2"
    assert record["recorded_at"] == 5000.0


def test_replacing_start_preserves_existing_terminal(state, monkeypatch):
    outbox = DecisionRecordOutbox(state)
    key = decision_key("t", "s", 0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "ultimate_coders.decision_record.time.time", lambda: clock["now"]
    )
    outbox.record_start(key, worker_id="w1")
    clock["now"] = 2000.0
    outbox.record_terminal(key, success=True, summary="done")
    clock["now"] = 3000.0
    outbox.record_start(key, worker_id="w2")

    record = outbox.history("t", "s")[0]
    assert record["worker_id"] == "w2"
    assert record["recorded_at"] == 3000.0
    assert record["terminal"] == {"success": True, "summary": "done"}
    assert record["terminal_at"] == 2000.0


def test_terminal_merges_and_always_restamps(state, monkeypatch):
    outbox = DecisionRecordOutbox(state)
    key = decision_key("t", "s", 0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "ultimate_coders.decision_record.time.time", lambda: clock["now"]
    )
    outbox.record_start(key, worker_id="w1")
    clock["now"] = 2000.0
    outbox.record_terminal(key, success=False, summary="boom", error_class="auth")
    clock["now"] = 3000.0
    outbox.record_terminal(key, success=False, summary="boom", error_class="auth")

    record = outbox.history("t", "s")[0]
    assert record["worker_id"] == "w1"
    assert record["recorded_at"] == 1000.0
    assert record["terminal"]["success"] is False
    assert record["terminal"]["error_class"] == "auth"
    assert record["terminal_at"] == 3000.0


def test_terminal_carries_optional_oracle_and_review(state):
    outbox = DecisionRecordOutbox(state)
    key = decision_key("t", "s", 0)
    outbox.record_start(key, worker_id="w1")
    outbox.record_terminal(
        key,
        success=True,
        summary="accepted",
        oracle={"accepted": True, "improvement_pct": 1.5},
        review={"approved": True, "issues": [], "suggestions": []},
    )

    terminal = outbox.history("t", "s")[0]["terminal"]
    assert terminal["oracle"]["accepted"] is True
    assert terminal["review"]["approved"] is True


def test_identity_route_is_omitted_from_stored_start(state):
    outbox = DecisionRecordOutbox(state)
    outbox.record_start(decision_key("t", "s", 0), worker_id="w1", route=None)

    record = outbox.history("t", "s")[0]
    assert "route" not in record


def test_governance_refs_are_stored_when_present(state):
    outbox = DecisionRecordOutbox(state)
    outbox.record_start(
        decision_key("t", "s", 0),
        worker_id="w1",
        governance={"mandate_id": "m-1", "grant": "read"},
    )

    record = outbox.history("t", "s")[0]
    assert record["governance"] == {"mandate_id": "m-1", "grant": "read"}


def test_history_isolates_subtasks_and_orders_by_key(state):
    outbox = DecisionRecordOutbox(state)
    outbox.record_start(decision_key("t", "s1", 0), worker_id="w")
    outbox.record_start(decision_key("t", "s2", 0), worker_id="w")
    outbox.record_start(decision_key("t", "s1", 1), worker_id="w")
    outbox.record_start(decision_key("t", "s10", 0), worker_id="w")

    assert [r["key"] for r in outbox.history("t", "s1")] == ["t:s1:0", "t:s1:1"]
    assert [r["key"] for r in outbox.history("t", "s10")] == ["t:s10:0"]


def test_total_counts_the_scan_not_the_window(state):
    outbox = DecisionRecordOutbox(state)
    for i in range(5):
        outbox.record_start(decision_key("t", "s", i), worker_id="w")

    assert outbox.total("t") == 5
    assert outbox.total("t", subtask_id="s") == 5
    assert len(outbox.history("t", "s", limit=2)) == 2


# ── worker integration ───────────────────────────────────────────


class _AuditWorker(Worker):
    MAX_RETRIES = 1
    RETRY_DELAYS = [0.0]


def _make_worker(monkeypatch, tmp_path) -> _AuditWorker:
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    worker = _AuditWorker(worker_id="w-audit", engine=None)

    async def _ok(subtask, context_block, workspace_handle):
        return SubtaskResult(
            subtask_id=subtask.id, worker_id="w-audit", summary="ok", success=True
        )

    worker._execute_in_sandbox = _ok  # type: ignore[assignment]

    async def _capture_event(event_type, **kw):
        return None

    worker._publish_event = _capture_event  # type: ignore[assignment]
    return worker


async def test_worker_records_start_and_terminal_on_success(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")
    await worker.execute_subtask(subtask)

    rows = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")
    assert len(rows) == 1
    record = rows[0]
    assert record["key"] == "t1:s1:0"
    assert record["worker_id"] == "w-audit"
    assert "route" not in record  # identity route omitted
    assert record["context_digest"]
    assert record["context_key"] == manifest_key("t1", "s1", 0)
    assert record["terminal"]["success"] is True
    assert record["terminal"]["summary"] == "ok"
    # Digest matches the context-manifest summary that was also stored.
    manifest = ContextManifestOutbox(RuntimeState()).history("t1", "s1")[0]
    assert record["context_digest"] == context_digest(manifest["summary"])


async def test_worker_records_non_identity_route(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)

    class _Router:
        def route_subtask(self, *args, **kwargs):
            return RouteDecision(
                stage="implementation",
                risk="medium",
                model="gpt-test",
                budget=ReasoningBudget(max_tokens=100),
                reason="table",
            )

    worker._model_router = _Router()
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")
    await worker.execute_subtask(subtask)

    record = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")[0]
    assert record["route"]["model"] == "gpt-test"
    assert record["route"]["stage"] == "implementation"


async def test_worker_records_terminal_on_final_failure(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)

    async def _fail(subtask, context_block, workspace_handle):
        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id="w-audit",
            summary="authentication failed: unauthorized",
            success=False,
            retryable=False,
            error="authentication failed: unauthorized",
        )

    worker._execute_in_sandbox = _fail  # type: ignore[assignment]
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")
    result = await worker.execute_subtask(subtask)

    assert result.success is False
    record = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")[0]
    assert record["terminal"]["success"] is False
    assert record["terminal"]["error_class"] == "auth"
    assert "authentication" in record["terminal"]["summary"]


async def test_worker_terminal_includes_domain_oracle_and_review(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)

    async def _ok(subtask, context_block, workspace_handle):
        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id="w-audit",
            summary="ok",
            success=True,
            domain_result={"oracle": {"accepted": True}},
            review=SubtaskReview(approved=True),
        )

    worker._execute_in_sandbox = _ok  # type: ignore[assignment]
    await worker.execute_subtask(
        Subtask(id="s1", parent_id="t1", description="fix the login bug")
    )

    terminal = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")[0]["terminal"]
    assert terminal["oracle"] == {"accepted": True}
    assert terminal["review"]["approved"] is True


async def test_decision_persistence_failure_does_not_fail_the_subtask(
    monkeypatch, tmp_path
):
    worker = _make_worker(monkeypatch, tmp_path)
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")

    def _boom(*args, **kwargs):
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr(RuntimeState, "mutate", _boom)

    result = await worker.execute_subtask(subtask)
    assert result.success is True
    assert result.summary == "ok"


async def test_worker_uses_dispatch_attempt_in_the_key(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="fix the login bug",
        dispatch_retry_count=3,
        retry_count=7,
    )
    await worker.execute_subtask(subtask)

    rows = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")
    assert [r["key"] for r in rows] == ["t1:s1:3"]


async def test_a_redispatch_keeps_the_earlier_attempts_record(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)
    first = Subtask(id="s1", parent_id="t1", description="fix the login bug")
    second = Subtask(
        id="s1",
        parent_id="t1",
        description="fix the login bug",
        dispatch_retry_count=2,
        retry_count=2,
    )
    await worker.execute_subtask(first)
    await worker.execute_subtask(second)

    outbox = DecisionRecordOutbox(RuntimeState())
    assert [r["key"] for r in outbox.history("t1", "s1")] == ["t1:s1:0", "t1:s1:2"]
    assert outbox.total("t1") == 2


async def test_gateway_context_block_records_start_without_context_digest(
    monkeypatch, tmp_path
):
    worker = _make_worker(monkeypatch, tmp_path)
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")
    await worker.execute_subtask(
        subtask,
        gateway_context_block={"entries": [{"node_id": "n-1", "summary": "committed"}]},
    )

    record = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")[0]
    assert record["worker_id"] == "w-audit"
    assert "context_digest" not in record
    assert "context_key" not in record
    assert record["terminal"]["success"] is True


async def test_worktree_allocation_failure_still_records_decision(monkeypatch, tmp_path):
    """D5: an attempt that dies at exclusive worktree acquire still gets a record."""
    worker = _make_worker(monkeypatch, tmp_path)

    class _NoWorktree:
        def bind_runtime_state(self, state):
            return None

        async def acquire(self, *args, **kwargs):
            return None

    worker._workspace_manager = _NoWorktree()
    monkeypatch.setattr(
        "ultimate_coders.inference.agent.InferenceInfraAgent.requires_workspace",
        staticmethod(lambda *a, **k: True),
    )
    monkeypatch.setattr(
        "ultimate_coders.inference.agent.InferenceInfraAgent.route",
        staticmethod(lambda *a, **k: None),
    )

    result = await worker.execute_subtask(
        Subtask(id="s1", parent_id="t1", description="fix the login bug")
    )

    assert result.success is False
    assert "worktree" in result.summary.lower()
    record = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")[0]
    assert record["key"] == "t1:s1:0"
    assert record["worker_id"] == "w-audit"
    assert record["terminal"]["success"] is False
    assert "worktree" in record["terminal"]["summary"].lower()


async def test_inference_quarantine_failure_still_records_terminal(monkeypatch, tmp_path):
    """D5: needs_worktree exception path finishes the attempt — terminal must merge."""
    worker = _make_worker(monkeypatch, tmp_path)

    class _Handle:
        workspace_id = "ws-1"

    class _QuarantineWM:
        def bind_runtime_state(self, state):
            return None

        async def acquire(self, *args, **kwargs):
            return _Handle()

        async def quarantine(self, handle):
            return None

        async def release(self, handle, merge=False):
            return None

    worker._workspace_manager = _QuarantineWM()
    monkeypatch.setattr(
        "ultimate_coders.inference.agent.InferenceInfraAgent.requires_workspace",
        staticmethod(lambda *a, **k: True),
    )
    monkeypatch.setattr(
        "ultimate_coders.inference.agent.InferenceInfraAgent.route",
        staticmethod(lambda *a, **k: None),
    )

    async def _boom(subtask, context_block, workspace_handle):
        raise RuntimeError("sandbox exploded")

    worker._execute_in_sandbox = _boom  # type: ignore[assignment]

    result = await worker.execute_subtask(
        Subtask(id="s1", parent_id="t1", description="fix the login bug")
    )

    assert result.success is False
    assert result.cleanup_pending is True
    record = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")[0]
    assert record["terminal"]["success"] is False
    assert "interrupted" in record["terminal"]["summary"].lower()


# ── local tool receipts: attempt-scoped workspace evidence (D17 / P7) ──


def test_workspace_summary_sorts_and_caps():
    from ultimate_coders.decision_record import (
        WORKSPACE_FILE_LIMIT,
        WORKSPACE_TOOL_LIMIT,
        workspace_summary,
    )

    files = [
        FileChange(file_path=f"src/f{i:02}.py", change_type=ChangeType.MODIFIED)
        for i in range(60)
    ]
    tools = [f"tool-{i}" for i in range(25)]
    summary = workspace_summary(files, tools)

    assert summary["file_count"] == 60
    assert len(summary["files"]) == WORKSPACE_FILE_LIMIT
    paths = [row["path"] for row in summary["files"]]
    assert paths == sorted(paths)
    assert summary["files"][0]["change_type"] == "modified"
    assert len(summary["tool_calls"]) == WORKSPACE_TOOL_LIMIT
    assert summary["truncated"] is True


def test_workspace_summary_is_deterministic_and_dedupes():
    from ultimate_coders.decision_record import workspace_summary

    files = [
        FileChange(file_path="b.py", change_type=ChangeType.CREATED),
        FileChange(file_path="a.py", change_type=ChangeType.DELETED),
        FileChange(file_path="b.py", change_type=ChangeType.CREATED),
    ]
    first = workspace_summary(files, ["Edit", "Bash", "Edit"])
    second = workspace_summary(list(reversed(files)), ["Edit", "Bash", "Edit"])
    assert first == second
    assert [row["path"] for row in first["files"]] == ["a.py", "b.py"]
    assert [row["change_type"] for row in first["files"]] == ["deleted", "created"]
    assert first["tool_calls"] == ["Edit", "Bash"]
    assert first["truncated"] is False


def test_workspace_summary_never_copies_diff_content():
    from ultimate_coders.decision_record import workspace_summary

    summary = workspace_summary(
        [FileChange(file_path="a.py", change_type=ChangeType.MODIFIED, diff="SECRET")],
        [],
    )
    assert "SECRET" not in json.dumps(summary)


def test_workspace_summary_drops_blank_paths_and_empty_evidence():
    from ultimate_coders.decision_record import workspace_summary

    assert workspace_summary([FileChange(file_path="  ")], []) is None
    assert workspace_summary([], []) is None
    assert workspace_summary([], ["", "  "]) is None


def test_terminal_workspace_is_stored_only_when_supplied(state):
    from ultimate_coders.decision_record import NAMESPACE

    outbox = DecisionRecordOutbox(state)
    key = decision_key("t", "s", 0)
    outbox.record_terminal(key, success=True, summary="ok")
    assert "workspace" not in state.get(NAMESPACE, key)["terminal"]

    workspace = {
        "files": [{"path": "a.py", "change_type": "modified"}],
        "file_count": 1,
        "tool_calls": ["Edit"],
        "truncated": False,
    }
    outbox.record_terminal(key, success=True, summary="ok", workspace=workspace)
    assert state.get(NAMESPACE, key)["terminal"]["workspace"] == workspace


async def test_worker_terminal_records_workspace_evidence(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)

    async def _with_changes(subtask, context_block, workspace_handle):
        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id="w-audit",
            summary="ok",
            success=True,
            modified_files=[
                FileChange(file_path="src/app.py", change_type=ChangeType.MODIFIED),
                FileChange(file_path="tests/test_app.py", change_type=ChangeType.CREATED),
            ],
            recent_tool_calls=["Edit", "Bash"],
        )

    worker._execute_in_sandbox = _with_changes  # type: ignore[assignment]
    await worker.execute_subtask(Subtask(id="s1", parent_id="t1", description="fix it"))

    record = DecisionRecordOutbox(RuntimeState()).history("t1", "s1")[0]
    workspace = record["terminal"]["workspace"]
    assert workspace["file_count"] == 2
    assert workspace["files"] == [
        {"path": "src/app.py", "change_type": "modified"},
        {"path": "tests/test_app.py", "change_type": "created"},
    ]
    assert workspace["tool_calls"] == ["Edit", "Bash"]
    assert workspace["truncated"] is False
