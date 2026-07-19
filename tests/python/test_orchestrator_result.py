"""F55 — handle_subtask_result must be idempotent.

Default-mode workers consume their own published uc.task.event loopback, so
a locally executed subtask's result lands twice (the _run_one direct call +
the loopback via _handle_remote_subtask_result with worker_id="remote").
Before the guard, the duplicate decremented worker load a second time
(stealing capacity accounting from concurrent subtasks) and re-fired merge
arbitration — two concurrent MergeArbiter runs merging into main.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ultimate_coders.agent.orchestrator import Orchestrator, WorkerEntry
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)


def _make_task(task_id: str, subtask_ids: list[str]) -> Task:
    return Task(
        id=task_id,
        description="test task",
        subtasks=[
            Subtask(id=sid, description=f"subtask {sid}", parent_id=task_id)
            for sid in subtask_ids
        ],
        status=TaskStatus.IN_PROGRESS,
    )


def _result(subtask_id: str, worker_id: str = "w-1", success: bool = True) -> SubtaskResult:
    return SubtaskResult(
        subtask_id=subtask_id,
        worker_id=worker_id,
        summary="done" if success else "boom",
        success=success,
    )


async def test_duplicate_result_does_not_double_decrement_load():
    orch = Orchestrator()
    task = _make_task("t-1", ["st-1", "st-2"])
    orch.tasks["t-1"] = task
    orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=2)
    orch.workers["remote"] = WorkerEntry(id="remote", current_load=0)

    # Local _run_one path applies the real result.
    await orch.handle_subtask_result(_result("st-1", worker_id="w-1"))
    assert orch.workers["w-1"].current_load == 1

    # Loopback event (worker_id="remote") for the SAME subtask must be a no-op:
    # load stays at 1 — the other running subtask keeps its capacity slot.
    await orch.handle_subtask_result(_result("st-1", worker_id="remote"))
    assert orch.workers["w-1"].current_load == 1
    assert orch.workers["remote"].current_load == 0


async def test_duplicate_result_fires_arbitration_only_once():
    orch = Orchestrator()
    task = _make_task("t-1", ["st-1"])
    orch.tasks["t-1"] = task
    orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)
    orch.merge_arbiter = MagicMock()
    orch._schedule_arbitration = MagicMock()

    await orch.handle_subtask_result(_result("st-1"))
    await orch.handle_subtask_result(_result("st-1", worker_id="remote"))  # loopback

    assert task.status == TaskStatus.COMPLETED
    orch._schedule_arbitration.assert_called_once()


async def test_retry_after_failure_still_applies_new_result():
    """Idempotency must not block legitimate retries: the subtask is reset to
    PENDING before re-execution, so the new result sees a non-terminal state.
    """
    orch = Orchestrator()
    task = _make_task("t-1", ["st-1"])
    orch.tasks["t-1"] = task
    orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

    await orch.handle_subtask_result(_result("st-1", success=False))
    st = task.subtasks[0]
    assert st.status == SubtaskStatus.FAILED

    # Retry reset (mirrors reset-to-pending on retry).
    st.status = SubtaskStatus.PENDING
    st.result = None

    await orch.handle_subtask_result(_result("st-1"))
    assert st.status == SubtaskStatus.COMPLETED
    assert st.result is not None and st.result.success is True
