"""P1b increment 2 — context-compilation manifests persisted for audit.

The compiler's ``summary()`` used to exist only inside the
``subtask_started`` event: miss the event and the answer to "why was this
prompt smaller than expected" was gone with the worker. These tests pin
the audit trail: one record per compile, attempt-scoped keys, latest
compile wins, no prompt content stored, and a persistence failure that
cannot fail the subtask it describes.
"""

from __future__ import annotations

import itertools

import pytest
from ultimate_coders.agent.context_compiler import (
    ContextItem,
    Tier,
    compile_context,
)
from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.agent.worker import Worker
from ultimate_coders.context_outbox import (
    ContextManifestOutbox,
    manifest_key,
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


# ── key identity ─────────────────────────────────────────────────


def test_manifest_key_includes_the_attempt():
    assert manifest_key("t", "s", 0) == "t:s:0"
    # The attempt is part of the identity on purpose (module docstring):
    # a retry that recompiles must not overwrite the earlier snapshot.
    assert manifest_key("t", "s", 0) != manifest_key("t", "s", 1)


# ── record / history ─────────────────────────────────────────────


def test_record_round_trips_the_summary(state):
    outbox = ContextManifestOutbox(state)
    summary = _summary()
    outbox.record(manifest_key("t", "s", 0), summary=summary)

    rows = outbox.history("t", "s")
    assert len(rows) == 1
    assert rows[0]["key"] == "t:s:0"
    assert rows[0]["summary"] == summary
    assert rows[0]["recorded_at"] > 0


def test_record_stores_only_the_summary_no_content(state):
    # The stored record must stay log-safe: exactly the summary's keys,
    # nothing the compiler chose not to expose.
    outbox = ContextManifestOutbox(state)
    summary = _summary()
    outbox.record(manifest_key("t", "s", 0), summary=summary)

    record = outbox.history("t", "s")[0]
    assert set(record) == {"key", "summary", "recorded_at"}
    assert set(record["summary"]) == {
        "budget_tokens",
        "total_tokens",
        "risk",
        "over_budget",
        "items_by_tier",
        "dropped",
    }


def test_repeat_record_with_same_summary_is_a_no_op(state, monkeypatch):
    # ``RuntimeState.mutate`` bumps the version on every call, so the
    # observable contract of "keep the existing record" is that the
    # stored content (including recorded_at) survives untouched. The
    # patched clock is global (mutate reads time.time too), hence the
    # unbounded counter rather than a fixed tick list.
    outbox = ContextManifestOutbox(state)
    summary = _summary()
    key = manifest_key("t", "s", 0)
    clock = itertools.count(1000.0)
    monkeypatch.setattr(
        "ultimate_coders.context_outbox.time.time", lambda: next(clock)
    )
    outbox.record(key, summary=summary)

    outbox.record(key, summary=summary)

    record = outbox.history("t", "s")[0]
    assert record["recorded_at"] == 1000.0
    assert record["summary"] == summary


def test_repeat_record_with_different_summary_keeps_the_latest(state):
    outbox = ContextManifestOutbox(state)
    key = manifest_key("t", "s", 0)
    outbox.record(key, summary=_summary(budget=100))
    outbox.record(key, summary=_summary(budget=200))

    # The latest compile is what the attempt actually used.
    assert outbox.history("t", "s")[0]["summary"]["budget_tokens"] == 200


def test_history_isolates_subtasks_and_orders_by_key(state):
    outbox = ContextManifestOutbox(state)
    outbox.record(manifest_key("t", "s1", 0), summary=_summary())
    outbox.record(manifest_key("t", "s2", 0), summary=_summary(budget=200))
    outbox.record(manifest_key("t", "s1", 1), summary=_summary(budget=300))
    # "s10" shares s1's prefix as a string but is a different subtask;
    # the trailing ":" in the scan prefix must keep them apart.
    outbox.record(manifest_key("t", "s10", 0), summary=_summary(budget=400))

    assert [r["key"] for r in outbox.history("t", "s1")] == ["t:s1:0", "t:s1:1"]
    assert [r["key"] for r in outbox.history("t", "s2")] == ["t:s2:0"]
    assert [r["key"] for r in outbox.history("t", "s10")] == ["t:s10:0"]


# ── worker integration ───────────────────────────────────────────


class _AuditWorker(Worker):
    MAX_RETRIES = 1
    RETRY_DELAYS = [0.0]


def _make_worker(monkeypatch, tmp_path) -> _AuditWorker:
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    worker = _AuditWorker(worker_id="w-audit", engine=None)

    async def _ok(subtask, context_block, workspace_handle):
        return SubtaskResult(subtask_id=subtask.id, worker_id="w-audit", summary="ok", success=True)

    worker._execute_in_sandbox = _ok  # type: ignore[assignment]

    async def _capture_event(event_type, **kw):
        return None

    worker._publish_event = _capture_event  # type: ignore[assignment]
    return worker


async def test_worker_persists_the_manifest_it_compiled(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")
    await worker.execute_subtask(subtask)

    state = RuntimeState()
    rows = ContextManifestOutbox(state).history("t1", "s1")
    assert len(rows) == 1
    assert rows[0]["summary"]["risk"] == "medium"
    assert rows[0]["summary"]["budget_tokens"] > 0


async def test_gateway_context_block_records_no_manifest(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")
    await worker.execute_subtask(
        subtask,
        gateway_context_block={"entries": [{"node_id": "n-1", "summary": "committed"}]},
    )

    # The gateway block path compiles nothing locally, so there is no
    # manifest to audit -- an empty history, not a fabricated record.
    state = RuntimeState()
    assert ContextManifestOutbox(state).history("t1", "s1") == []


async def test_persistence_failure_does_not_fail_the_subtask(monkeypatch, tmp_path):
    worker = _make_worker(monkeypatch, tmp_path)
    subtask = Subtask(id="s1", parent_id="t1", description="fix the login bug")

    def _boom(*args, **kwargs):
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr(RuntimeState, "mutate", _boom)

    result = await worker.execute_subtask(subtask)
    assert result.success is True
    assert result.summary == "ok"
