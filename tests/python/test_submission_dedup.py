"""Tests for PR01's consumer-side submit dedup.

The gateway publishes a submission at-least-once (transactional outbox + crash
recovery). These tests pin the other half of the contract: the consumer applies a
submission once, drops a re-delivery, and — crucially — does NOT drop a
submission whose previous attempt never reached the orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest
from ultimate_coders.runtime_state import RuntimeState
from ultimate_coders.submission_dedup import (
    MAX_RECORDED_ATTEMPTS,
    NAMESPACE,
    SubmissionDedup,
    record_key,
    submission_identity,
)


@pytest.fixture()
def state(tmp_path: pathlib.Path) -> RuntimeState:
    return RuntimeState(path=tmp_path / "dedup.db")


@pytest.fixture()
def dedup(state: RuntimeState) -> SubmissionDedup:
    return SubmissionDedup(state)


def test_identity_reads_submission_and_task_ids() -> None:
    assert submission_identity({"submission_id": "sub-1", "task_id": "task-1"}) == (
        "sub-1",
        "task-1",
    )
    assert submission_identity({}) == ("", "")
    assert submission_identity({"task_id": " task-1 "}) == ("", "task-1")


def test_record_key_requires_a_task_id() -> None:
    assert record_key("sub-1", "task-1") == "sub-1|task-1"
    assert record_key("", "task-1") == "task:task-1"
    assert record_key("sub-1", "") is None


def test_first_observation_is_not_a_duplicate_and_counts_the_attempt(
    dedup: SubmissionDedup,
) -> None:
    claim = dedup.observe("sub-1", "task-1")
    assert claim.duplicate is False
    assert claim.attempts == 1
    assert dedup.is_applied("sub-1", "task-1") is False


def test_applied_submission_is_reported_as_duplicate(dedup: SubmissionDedup) -> None:
    dedup.observe("sub-1", "task-1")
    dedup.mark_applied("sub-1", "task-1", orchestrator_task_id="task-1")
    second = dedup.observe("sub-1", "task-1")
    assert second.duplicate is True
    assert second.attempts == 2
    assert second.applied_task_id == "task-1"


def test_crash_between_observe_and_apply_does_not_drop_the_retry(
    dedup: SubmissionDedup,
) -> None:
    # Attempt 1 observed but never applied (simulated crash).
    dedup.observe("sub-1", "task-1")
    retry = dedup.observe("sub-1", "task-1")
    assert retry.duplicate is False, (
        "an unapplied submission must be processed again — dropping it would lose work"
    )
    dedup.mark_applied("sub-1", "task-1")
    assert dedup.observe("sub-1", "task-1").duplicate is True


def test_distinct_submissions_are_independent(dedup: SubmissionDedup) -> None:
    dedup.observe("sub-1", "task-1")
    dedup.mark_applied("sub-1", "task-1")
    # Same task id, different submission id: a genuinely different request.
    other = dedup.observe("sub-2", "task-1")
    assert other.duplicate is False
    # Same submission id, different task id: the gateway derives one task id, so
    # this can only happen across gateways; treat it as independent work.
    third = dedup.observe("sub-1", "task-2")
    assert third.duplicate is False


def test_legacy_payload_without_submission_id_still_dedupes_by_task(
    dedup: SubmissionDedup,
) -> None:
    dedup.observe("", "task-legacy")
    dedup.mark_applied("", "task-legacy")
    assert dedup.observe("", "task-legacy").duplicate is True


def test_payload_without_task_id_is_not_deduped(dedup: SubmissionDedup) -> None:
    claim = dedup.observe("sub-1", "")
    assert claim.duplicate is False
    assert claim.attempts == 0
    dedup.mark_applied("sub-1", "")
    assert dedup.observe("sub-1", "").duplicate is False


def test_attempt_counter_is_bounded(state: RuntimeState) -> None:
    dedup = SubmissionDedup(state)
    for _ in range(MAX_RECORDED_ATTEMPTS + 5):
        dedup.observe("sub-1", "task-1")
    record = state.get(NAMESPACE, "sub-1|task-1")
    assert record is not None
    assert record["attempts"] == MAX_RECORDED_ATTEMPTS


def test_dedup_state_survives_a_new_handle(state: RuntimeState) -> None:
    SubmissionDedup(state).observe("sub-1", "task-1")
    SubmissionDedup(state).mark_applied("sub-1", "task-1")
    # A fresh process-level handle (worker restart) must still see the record.
    assert SubmissionDedup(state).observe("sub-1", "task-1").duplicate is True


class _FakeMsg:
    def __init__(self, payload: dict, reply: str = "") -> None:
        self.data = json.dumps(payload).encode()
        self.reply = reply
        self.responses: list[bytes] = []

    async def respond(self, data: bytes) -> None:
        self.responses.append(data)


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.status = type("S", (), {"value": "Planning"})()
        self.subtasks: list = []


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def submit_task(self, description: str, **kwargs):
        self.calls.append((description, kwargs))
        return _FakeTask(kwargs.get("task_id") or "task-generated")


def _fake_worker(orchestrator: _FakeOrchestrator, state: RuntimeState):
    """A worker object with only the attributes `_handle_submit` touches."""

    from ultimate_coders.nats_worker import NatsWorker

    worker = object.__new__(NatsWorker)
    worker._orchestrator = orchestrator
    worker._runtime_state = state
    worker._cancelled_task_ids = set()
    worker._running_node_tasks = {}
    worker._publisher = None
    snapshots: list = []

    async def _snapshot(task):
        snapshots.append(task.id)

    worker._publish_task_snapshot = _snapshot  # type: ignore[assignment]
    worker._spawn_bg = lambda coro: coro.close()  # type: ignore[assignment]
    worker._snapshots = snapshots
    return worker


def test_handler_drops_a_redelivered_submission(
    state: RuntimeState,
) -> None:
    orchestrator = _FakeOrchestrator()
    worker = _fake_worker(orchestrator, state)
    payload = {
        "task_id": "task-1",
        "description": "fix the build",
        "project_id": "proj",
        "submission_id": "sub-1",
    }

    asyncio.run(worker._handle_submit(_FakeMsg(payload)))
    assert len(orchestrator.calls) == 1

    duplicate_msg = _FakeMsg(payload, reply="inbox")
    asyncio.run(worker._handle_submit(duplicate_msg))
    assert len(orchestrator.calls) == 1, "the duplicate must not be decomposed again"
    assert duplicate_msg.responses, "a request-reply duplicate must still answer"
    assert json.loads(duplicate_msg.responses[0])["duplicate"] is True


def test_handler_reprocesses_when_the_previous_attempt_never_applied(
    state: RuntimeState,
) -> None:
    orchestrator = _FakeOrchestrator()
    worker = _fake_worker(orchestrator, state)
    # Simulate a crash after observation but before the orchestrator call.
    SubmissionDedup(state).observe("sub-1", "task-1")

    asyncio.run(
        worker._handle_submit(
            _FakeMsg(
                {
                    "task_id": "task-1",
                    "description": "fix the build",
                    "project_id": "proj",
                    "submission_id": "sub-1",
                }
            )
        )
    )
    assert len(orchestrator.calls) == 1
    assert SubmissionDedup(state).is_applied("sub-1", "task-1")
