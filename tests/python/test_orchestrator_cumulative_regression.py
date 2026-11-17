"""V10 #17 — observed ancestor-F2P regressions block descendant completion."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)


def _task_with_measurement(
    *,
    ancestor_f2p: list[str],
    ancestor_status: str = "ok",
) -> tuple[Orchestrator, Task, Subtask]:
    task = Task(
        id="task-cumulative",
        description="cumulative regression test",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[
            Subtask(
                id="ancestor",
                parent_id="task-cumulative",
                description="ancestor",
                status=SubtaskStatus.COMPLETED,
            ),
            Subtask(
                id="descendant",
                parent_id="task-cumulative",
                description="descendant",
                depends_on=["ancestor"],
                status=SubtaskStatus.ASSIGNED,
            ),
        ],
    )
    task.subtasks[0].result = SubtaskResult(
        subtask_id="ancestor",
        worker_id="w-1",
        summary="ancestor done",
        success=True,
        test_execution={
            "status": ancestor_status,
            "f2p_tests": ancestor_f2p,
            "p2p_tests": [],
            "broken": [],
        },
    )
    orch = Orchestrator()
    orch.tasks[task.id] = task
    orch._schedule_aggregation = MagicMock()
    return orch, task, task.subtasks[1]


def _measured_result(*, broken: list[str], status: str = "ok") -> SubtaskResult:
    return SubtaskResult(
        subtask_id="descendant",
        worker_id="w-1",
        summary="descendant done",
        success=True,
        test_execution={
            "status": status,
            "f2p_tests": [],
            "p2p_tests": [],
            "broken": broken,
        },
    )


def test_cumulative_regression_blocks_descendant_completion() -> None:
    orch, _task, descendant = _task_with_measurement(
        ancestor_f2p=["tests/test_api.py::test_get"],
    )

    error = orch._cumulative_regression_error(
        orch.tasks["task-cumulative"],
        descendant,
        _measured_result(broken=["tests/test_api.py::test_get"]),
    )

    assert error == (
        "Cumulative regression: tests/test_api.py::test_get passed for ancestor "
        "ancestor but is broken now (1 inherited test(s) failed)"
    )


@pytest.mark.asyncio
async def test_handle_subtask_result_marks_cumulative_failure() -> None:
    orch, task, _descendant = _task_with_measurement(
        ancestor_f2p=["tests/test_api.py::test_get"],
    )

    await orch.handle_subtask_result(
        _measured_result(broken=["tests/test_api.py::test_get"]),
    )

    descendant = task.subtasks[1]
    assert descendant.status is SubtaskStatus.FAILED
    assert descendant.result is not None
    assert descendant.result.error.startswith("Cumulative regression:")


def test_cumulative_regression_ignores_missing_or_unbroken_data() -> None:
    orch, task, descendant = _task_with_measurement(
        ancestor_f2p=["tests/test_api.py::test_get"],
    )

    assert orch._cumulative_regression_error(task, descendant, _measured_result(broken=[])) is None
    assert orch._cumulative_regression_error(
        task,
        descendant,
        _measured_result(broken=["tests/test_api.py::test_get"], status="error"),
    ) is None
    missing = SubtaskResult(subtask_id="descendant", success=True)
    assert orch._cumulative_regression_error(task, descendant, missing) is None


def test_cumulative_regression_inherits_transitive_ancestor_f2p() -> None:
    task = Task(
        id="task-transitive",
        description="transitive",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[
            Subtask(
                id="a",
                parent_id="task-transitive",
                status=SubtaskStatus.COMPLETED,
            ),
            Subtask(
                id="b",
                parent_id="task-transitive",
                depends_on=["a"],
                status=SubtaskStatus.COMPLETED,
            ),
            Subtask(
                id="c",
                parent_id="task-transitive",
                depends_on=["b"],
                status=SubtaskStatus.ASSIGNED,
            ),
        ],
    )
    task.subtasks[0].result = SubtaskResult(
        subtask_id="a",
        success=True,
        test_execution={"status": "ok", "f2p_tests": ["tests/test_root.py::test_root"]},
    )
    task.subtasks[1].result = SubtaskResult(
        subtask_id="b",
        success=True,
        test_execution={"status": "ok", "f2p_tests": []},
    )
    orch = Orchestrator()

    error = orch._cumulative_regression_error(
        task,
        task.subtasks[2],
        _measured_result(broken=["tests/test_root.py::test_root"]),
    )

    assert error is not None
    assert "ancestor a" in error


def test_checkpoint_roundtrip_preserves_test_execution() -> None:
    orch, task, _descendant = _task_with_measurement(
        ancestor_f2p=["tests/test_api.py::test_get"],
    )

    restored = Task.from_dict(task.to_dict())

    assert restored.subtasks[0].result is not None
    assert restored.subtasks[0].result.test_execution == {
        "status": "ok",
        "f2p_tests": ["tests/test_api.py::test_get"],
        "p2p_tests": [],
        "broken": [],
    }
