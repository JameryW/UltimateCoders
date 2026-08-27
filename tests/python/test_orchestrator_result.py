"""F55 — handle_subtask_result must be idempotent.

Default-mode workers consume their own published uc.task.event loopback, so
a locally executed subtask's result lands twice (the _run_one direct call +
the loopback via _handle_remote_subtask_result with worker_id="remote").
Before the guard, the duplicate decremented worker load a second time
(stealing capacity accounting from concurrent subtasks) and re-fired merge
arbitration — two concurrent MergeArbiter runs merging into main.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.agent.aggregator import AggregatedResult, AggregationStatus
from ultimate_coders.agent.orchestrator import Orchestrator, WorkerEntry
from ultimate_coders.agent.types import (
    ChangeType,
    FileChange,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)
from ultimate_coders.nats_worker import NatsPublisher


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


def _result_with_files(
    subtask_id: str,
    files: list[FileChange],
    worker_id: str = "w-1",
) -> SubtaskResult:
    return SubtaskResult(
        subtask_id=subtask_id,
        worker_id=worker_id,
        modified_files=files,
        summary=f"{subtask_id} done",
        success=True,
    )


def _change(path: str, diff: str = "diff content") -> FileChange:
    return FileChange(file_path=path, change_type=ChangeType.MODIFIED, diff=diff)


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


async def test_subtask_result_publishes_terminal_parent_task_snapshot():
    """A terminal subtask result must publish the parent's terminal status.

    The gateway only sees Python state through ``uc.task.update``.  Updating
    the in-memory task without publishing the resulting parent snapshot leaves
    the gateway at InProgress until heartbeat cleanup marks it Failed.
    """
    publisher = MagicMock()
    publisher.publish_update = AsyncMock()
    orch = Orchestrator(nats_publisher=publisher)
    task = _make_task("t-publish-terminal", ["st-1"])
    orch.tasks[task.id] = task
    orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

    await orch.handle_subtask_result(_result("st-1"))

    publisher.publish_update.assert_awaited_once_with(task)
    assert publisher.publish_update.call_args.args[0].status == TaskStatus.COMPLETED


async def test_failed_subtask_result_publishes_failed_parent_task_snapshot():
    """A terminal failed subtask must publish a Failed parent snapshot."""
    publisher = MagicMock()
    publisher.publish_update = AsyncMock()
    orch = Orchestrator(nats_publisher=publisher)
    task = _make_task("t-publish-failed", ["st-1"])
    orch.tasks[task.id] = task
    orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

    await orch.handle_subtask_result(_result("st-1", success=False))

    publisher.publish_update.assert_awaited_once_with(task)
    assert publisher.publish_update.call_args.args[0].status == TaskStatus.FAILED


async def test_parent_task_snapshot_retries_transient_publish_failure():
    """A transient NATS failure must not lose the terminal parent snapshot."""
    publisher = MagicMock()
    publisher.publish_update = AsyncMock(
        side_effect=[ConnectionError("temporary"), None],
    )
    orch = Orchestrator(nats_publisher=publisher)
    task = _make_task("t-publish-retry", ["st-1"])
    orch.tasks[task.id] = task
    orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

    await orch.handle_subtask_result(_result("st-1"))

    assert publisher.publish_update.await_count == 2
    assert task.status == TaskStatus.COMPLETED


async def test_parent_task_snapshot_retries_publisher_false_result():
    """The real NatsPublisher's false result must trigger the same retry path."""
    nc = MagicMock()
    nc.publish = AsyncMock(side_effect=[ConnectionError("temporary"), None])
    orch = Orchestrator(nats_publisher=NatsPublisher(nc))
    task = _make_task("t-publish-false-retry", ["st-1"])
    orch.tasks[task.id] = task
    orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

    await orch.handle_subtask_result(_result("st-1"))

    assert nc.publish.await_count == 2
    assert task.status == TaskStatus.COMPLETED


# ── ResultAggregator wiring tests ──────────────────────────────


class TestAggregatorWiring:
    """Verify the ResultAggregator is instantiated and called at task completion."""

    def test_aggregator_instantiated_in_init(self):
        """Orchestrator must create a ResultAggregator in __init__."""
        orch = Orchestrator()
        assert orch.aggregator is not None
        assert hasattr(orch, "_pending_aggregation")
        assert orch._pending_aggregation == set()

    async def test_aggregation_scheduled_on_task_completion(self):
        """When all subtasks complete, _schedule_aggregation is called."""
        orch = Orchestrator()
        task = _make_task("t-agg", ["st-a", "st-b"])
        orch.tasks["t-agg"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=2)

        orch._schedule_aggregation = MagicMock()

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff-a")],
        ))
        await orch.handle_subtask_result(_result_with_files(
            "st-b", [_change("f.py", "diff-b")],
        ))

        assert task.status == TaskStatus.COMPLETED
        orch._schedule_aggregation.assert_called_once_with(task)

    async def test_aggregation_not_scheduled_on_failure(self):
        """If a subtask fails (task -> FAILED), aggregation should NOT fire."""
        orch = Orchestrator()
        task = _make_task("t-fail", ["st-a", "st-b"])
        orch.tasks["t-fail"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=2)
        orch._schedule_aggregation = MagicMock()

        await orch.handle_subtask_result(_result("st-a", success=False))
        await orch.handle_subtask_result(_result("st-b"))

        assert task.status == TaskStatus.FAILED
        orch._schedule_aggregation.assert_not_called()

    async def test_aggregation_runs_before_arbitration(self):
        """Both fire on completion; aggregation is scheduled first."""
        orch = Orchestrator()
        task = _make_task("t-ord", ["st-a"])
        orch.tasks["t-ord"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)
        orch.merge_arbiter = MagicMock()

        call_order: list[str] = []

        def mock_schedule_agg(task_arg):
            call_order.append("aggregation")

        def mock_schedule_arb(task_arg):
            call_order.append("arbitration")

        orch._schedule_aggregation = mock_schedule_agg
        orch._schedule_arbitration = mock_schedule_arb

        await orch.handle_subtask_result(_result("st-a"))

        assert task.status == TaskStatus.COMPLETED
        assert call_order == ["aggregation", "arbitration"]


class TestAggregationAdvisoryBehavior:
    """Verify aggregation is advisory — conflicts logged, task still completes, no writes."""

    async def test_aggregation_called_with_subtask_results(self):
        """The aggregator.aggregate() method is called with the collected results."""
        orch = Orchestrator()
        task = _make_task("t-call", ["st-a", "st-b"])
        orch.tasks["t-call"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=2)

        # Mock the aggregator's aggregate to track the call.
        mock_result = AggregatedResult(
            status=AggregationStatus.SUCCESS,
            merged_files=[],
            conflict_files=[],
        )
        orch.aggregator.aggregate = AsyncMock(return_value=mock_result)

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff-a")],
        ))
        await orch.handle_subtask_result(_result_with_files(
            "st-b", [_change("g.py", "diff-b")],
        ))

        # Wait for the fire-and-forget task to complete.
        await asyncio.sleep(0.05)

        orch.aggregator.aggregate.assert_called_once()
        call_args = orch.aggregator.aggregate.call_args
        results_arg = call_args[0][0]
        assert len(results_arg) == 2
        assert {r.subtask_id for r in results_arg} == {"st-a", "st-b"}

    async def test_conflict_does_not_abort_task(self):
        """Aggregation surfacing conflicts does NOT change task status."""
        orch = Orchestrator()
        task = _make_task("t-conf", ["st-a", "st-b"])
        orch.tasks["t-conf"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=2)

        # Mock aggregator to return a conflict result.
        conflict_result = AggregatedResult(
            status=AggregationStatus.CONFLICT,
            merged_files=[],
            conflict_files=["f.py"],
        )
        orch.aggregator.aggregate = AsyncMock(return_value=conflict_result)

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff-a")],
        ))
        await orch.handle_subtask_result(_result_with_files(
            "st-b", [_change("f.py", "diff-b")],
        ))

        # Task should still be COMPLETED despite conflict.
        assert task.status == TaskStatus.COMPLETED

    async def test_aggregation_exception_is_non_fatal(self):
        """If aggregate() raises, the task still completes (non-fatal)."""
        orch = Orchestrator()
        task = _make_task("t-exc", ["st-a"])
        orch.tasks["t-exc"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

        orch.aggregator.aggregate = AsyncMock(side_effect=RuntimeError("boom"))

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff-a")],
        ))

        # Wait for fire-and-forget to complete.
        await asyncio.sleep(0.05)

        # Task still completed despite aggregation failure.
        assert task.status == TaskStatus.COMPLETED

    async def test_aggregation_does_not_write_merged_content(self):
        """Advisory only — merged content is NOT written to disk."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up a fake merge_arbiter with a project_path so base_files
            # can be sourced.
            fake_arbiter = MagicMock()
            fake_arbiter._project_path = tmpdir

            orch = Orchestrator(merge_arbiter=fake_arbiter)
            task = _make_task("t-write", ["st-a", "st-b"])
            orch.tasks["t-write"] = task
            orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=2)

            # Both subtasks modify the same file — aggregator returns merged
            # content, but it must NOT be written.
            merged_content = "MERGED CONTENT THAT SHOULD NOT BE WRITTEN"
            mock_result = AggregatedResult(
                status=AggregationStatus.SUCCESS,
                merged_files=[FileChange(
                    file_path="f.py", diff=merged_content,
                )],
                conflict_files=[],
            )
            orch.aggregator.aggregate = AsyncMock(return_value=mock_result)

            await orch.handle_subtask_result(_result_with_files(
                "st-a", [_change("f.py", "diff-a")],
            ))
            await orch.handle_subtask_result(_result_with_files(
                "st-b", [_change("f.py", "diff-b")],
            ))

            await asyncio.sleep(0.05)

            # Verify no file was written with the merged content.
            import os

            for root, _dirs, files in os.walk(tmpdir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    with open(fpath, encoding="utf-8") as f:
                        content = f.read()
                    assert merged_content not in content, (
                        f"Merged content was written to {fpath} — "
                        "aggregation must be advisory (no writes)"
                    )


class TestAggregationBaseFilesSourcing:
    """Verify base_files sourcing from the MergeArbiter's project path."""

    async def test_base_files_empty_without_merge_arbiter(self):
        """No merge_arbiter → base_files = {} (empty base)."""
        orch = Orchestrator()
        results = [_result_with_files("s1", [_change("f.py", "diff")])]

        base_files = orch._collect_base_files(results)
        assert base_files == {}

    async def test_base_files_read_from_project_path(self):
        """base_files sourced from merge_arbiter._project_path."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file in the project path.
            import os

            fpath = os.path.join(tmpdir, "main.py")
            with open(fpath, "w") as f:
                f.write("original content\n")

            fake_arbiter = MagicMock()
            fake_arbiter._project_path = tmpdir

            orch = Orchestrator(merge_arbiter=fake_arbiter)
            results = [
                _result_with_files("s1", [_change("main.py", "changed")]),
            ]

            base_files = orch._collect_base_files(results)
            assert "main.py" in base_files
            assert base_files["main.py"] == "original content\n"

    async def test_base_files_skips_missing_files(self):
        """Files that don't exist in the project path are skipped (not error)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_arbiter = MagicMock()
            fake_arbiter._project_path = tmpdir

            orch = Orchestrator(merge_arbiter=fake_arbiter)
            results = [
                _result_with_files(
                    "s1",
                    [_change("nonexistent.py", "diff"), _change("other.py", "diff")],
                ),
            ]

            base_files = orch._collect_base_files(results)
            # Neither file exists → empty dict, no errors.
            assert base_files == {}


class TestAggregationFireAndForget:
    """Verify the fire-and-forget pattern doesn't block and cleans up."""

    async def test_pending_aggregation_cleared_after_completion(self):
        """The _pending_aggregation set is cleared after the task completes."""
        orch = Orchestrator()
        task = _make_task("t-ff", ["st-a"])
        orch.tasks["t-ff"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

        orch.aggregator.aggregate = AsyncMock(return_value=AggregatedResult())

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff")],
        ))

        # Wait for the fire-and-forget task to complete.
        await asyncio.sleep(0.05)

        # The set should be empty (done_callback discards).
        assert len(orch._pending_aggregation) == 0

    async def test_aggregation_does_not_block_task_completion(self):
        """Task status is set to COMPLETED before aggregation finishes."""
        orch = Orchestrator()
        task = _make_task("t-nb", ["st-a"])
        orch.tasks["t-nb"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

        # Make aggregate slow — if it blocked, task completion would hang.
        async def slow_aggregate(*args, **kwargs):
            await asyncio.sleep(1.0)
            return AggregatedResult()

        orch.aggregator.aggregate = slow_aggregate

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff")],
        ))

        # Task is already COMPLETED — aggregation is fire-and-forget.
        assert task.status == TaskStatus.COMPLETED

        # Clean up the pending task to avoid warnings.
        for t in orch._pending_aggregation:
            t.cancel()
        orch._pending_aggregation.clear()


class TestVerifyCommandThreading:
    """Verify verify_command flows submit_task → Task → _aggregate_results → aggregate()."""

    async def test_submit_task_stores_verify_command_on_task(self):
        """submit_task(verify_command=...) stores it on the returned Task."""
        orch = Orchestrator()
        task = await orch.submit_task(
            "do thing",
            task_id="t-vc-1",
            verify_command="cargo check",
        )
        assert task.verify_command == "cargo check"
        assert orch.tasks["t-vc-1"].verify_command == "cargo check"

    async def test_submit_task_defaults_verify_command_none(self):
        """Without verify_command, the Task field is None (no verification)."""
        orch = Orchestrator()
        task = await orch.submit_task("do thing", task_id="t-vc-2")
        assert task.verify_command is None

    async def test_aggregate_called_with_verify_command(self):
        """_aggregate_results passes the task's verify_command to aggregate()."""
        orch = Orchestrator()
        task = _make_task("t-vc-3", ["st-a"])
        task.verify_command = "cargo test"
        orch.tasks["t-vc-3"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

        mock_result = AggregatedResult(
            status=AggregationStatus.SUCCESS,
            verification_passed=True,
        )
        orch.aggregator.aggregate = AsyncMock(return_value=mock_result)

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff")],
        ))
        await asyncio.sleep(0.05)

        orch.aggregator.aggregate.assert_called_once()
        kwargs = orch.aggregator.aggregate.call_args.kwargs
        assert kwargs.get("verify_command") == "cargo test"

    async def test_aggregate_called_without_verify_command_when_none(self):
        """When task.verify_command is None, aggregate gets verify_command=None."""
        orch = Orchestrator()
        task = _make_task("t-vc-4", ["st-a"])
        orch.tasks["t-vc-4"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

        mock_result = AggregatedResult(status=AggregationStatus.SUCCESS)
        orch.aggregator.aggregate = AsyncMock(return_value=mock_result)

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff")],
        ))
        await asyncio.sleep(0.05)

        orch.aggregator.aggregate.assert_called_once()
        kwargs = orch.aggregator.aggregate.call_args.kwargs
        assert kwargs.get("verify_command") is None

    async def test_verification_passed_logged_on_success(self):
        """When verify_command is set and aggregate returns verification_passed,
        the result is logged (no crash)."""
        orch = Orchestrator()
        task = _make_task("t-vc-5", ["st-a"])
        task.verify_command = "true"
        orch.tasks["t-vc-5"] = task
        orch.workers["w-1"] = WorkerEntry(id="w-1", current_load=1)

        mock_result = AggregatedResult(
            status=AggregationStatus.SUCCESS,
            verification_passed=True,
        )
        orch.aggregator.aggregate = AsyncMock(return_value=mock_result)

        await orch.handle_subtask_result(_result_with_files(
            "st-a", [_change("f.py", "diff")],
        ))
        await asyncio.sleep(0.05)

        # Task completed, no crash.
        assert task.status == TaskStatus.COMPLETED

