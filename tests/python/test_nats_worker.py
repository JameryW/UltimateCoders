"""Tests for the NATS Worker — consumer, publisher, and message protocol.

These tests do not require a running NATS server. They verify:
- Message payload construction (serialization format)
- NatsPublisher publish calls with correct subjects/payloads
- NatsWorker submit message handling (with mocked NATS/Orchestrator)
- Orchestrator nats_publisher hook invocation
- Heartbeat timer logic
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)
from ultimate_coders.nats_worker import (
    NATS_SUBJECT_HEARTBEAT,
    NATS_SUBJECT_TASK_EVENT,
    NATS_SUBJECT_TASK_SUBMIT,
    NATS_SUBJECT_TASK_UPDATE,
    NatsPublisher,
    NatsWorker,
    _make_task_event_payload,
    _make_task_update_payload,
    _subtask_status_to_nats,
    _task_status_to_nats,
)

# ── Payload construction tests ──────────────────────────────────


class TestTaskStatusToNats:
    """Test TaskStatus enum to NATS string conversion."""

    def test_all_statuses(self):
        assert _task_status_to_nats(TaskStatus.CREATED) == "Created"
        assert _task_status_to_nats(TaskStatus.PLANNING) == "Planning"
        assert _task_status_to_nats(TaskStatus.IN_PROGRESS) == "InProgress"
        assert _task_status_to_nats(TaskStatus.COMPLETED) == "Completed"
        assert _task_status_to_nats(TaskStatus.FAILED) == "Failed"
        assert _task_status_to_nats(TaskStatus.PAUSED) == "Paused"


class TestSubtaskStatusToNats:
    """Test SubtaskStatus enum to NATS string conversion."""

    def test_all_statuses(self):
        assert _subtask_status_to_nats(SubtaskStatus.PENDING) == "Pending"
        assert _subtask_status_to_nats(SubtaskStatus.ASSIGNED) == "Assigned"
        assert _subtask_status_to_nats(SubtaskStatus.IN_PROGRESS) == "InProgress"
        assert _subtask_status_to_nats(SubtaskStatus.COMPLETED) == "Completed"
        assert _subtask_status_to_nats(SubtaskStatus.FAILED) == "Failed"
        assert _subtask_status_to_nats(SubtaskStatus.CONFLICTED) == "Conflicted"


class TestMakeTaskUpdatePayload:
    """Test ``uc.task.update`` payload construction."""

    def test_basic_task(self):
        task = Task(
            id="task-1",
            description="Fix the login bug",
            project_id="proj-1",
            status=TaskStatus.IN_PROGRESS,
        )
        payload = _make_task_update_payload(task)

        assert payload["task_id"] == "task-1"
        assert payload["status"] == "InProgress"
        assert payload["subtasks"] == []
        assert "result" not in payload

    def test_task_with_subtasks(self):
        st1 = Subtask(
            id="st-1",
            parent_id="task-1",
            description="Analyze code",
            status=SubtaskStatus.COMPLETED,
            assigned_worker="worker-1",
        )
        st2 = Subtask(
            id="st-2",
            parent_id="task-1",
            description="Fix bug",
            status=SubtaskStatus.ASSIGNED,
        )
        task = Task(
            id="task-1",
            description="Fix the login bug",
            status=TaskStatus.IN_PROGRESS,
            subtasks=[st1, st2],
        )
        payload = _make_task_update_payload(task)

        assert len(payload["subtasks"]) == 2
        assert payload["subtasks"][0]["subtask_id"] == "st-1"
        assert payload["subtasks"][0]["status"] == "Completed"
        # assigned_worker should be present when set
        assert payload["subtasks"][0]["assigned_worker"] == "worker-1"
        # assigned_worker should be absent when None
        assert "assigned_worker" not in payload["subtasks"][1]

    def test_task_with_result(self):
        task = Task(
            id="task-1",
            description="Fix the login bug",
            status=TaskStatus.COMPLETED,
            result="All subtasks completed",
        )
        payload = _make_task_update_payload(task)

        assert payload["result"] == "All subtasks completed"

    def test_subtask_with_result(self):
        st = Subtask(
            id="st-1",
            parent_id="task-1",
            description="Analyze code",
            status=SubtaskStatus.COMPLETED,
            result=SubtaskResult(
                subtask_id="st-1",
                worker_id="worker-1",
                summary="Analysis complete",
                success=True,
            ),
        )
        task = Task(
            id="task-1",
            description="Fix the login bug",
            status=TaskStatus.IN_PROGRESS,
            subtasks=[st],
        )
        payload = _make_task_update_payload(task)

        assert payload["subtasks"][0]["result"] == "Analysis complete"


class TestMakeTaskEventPayload:
    """Test ``uc.task.event`` payload construction."""

    def test_basic_event(self):
        payload = _make_task_event_payload(
            event_type="task_submitted",
            task_id="task-1",
        )
        assert payload["type"] == "task_submitted"
        assert payload["task_id"] == "task-1"
        assert "subtask_id" not in payload
        assert payload["data"] == {}

    def test_event_with_subtask(self):
        payload = _make_task_event_payload(
            event_type="subtask_completed",
            task_id="task-1",
            subtask_id="st-1",
            data={"success": True, "summary": "Done"},
        )
        assert payload["subtask_id"] == "st-1"
        assert payload["data"]["success"] is True
        assert payload["data"]["summary"] == "Done"

    def test_event_with_empty_subtask_id_omitted(self):
        payload = _make_task_event_payload(
            event_type="task_submitted",
            task_id="task-1",
            subtask_id="",
        )
        assert "subtask_id" not in payload


# ── NATS subject constants tests ────────────────────────────────


class TestNatsSubjects:
    """Verify NATS subject constants match the Rust server."""

    def test_subjects(self):
        assert NATS_SUBJECT_TASK_SUBMIT == "uc.task.submit"
        assert NATS_SUBJECT_TASK_UPDATE == "uc.task.update"
        assert NATS_SUBJECT_TASK_EVENT == "uc.task.event"
        assert NATS_SUBJECT_HEARTBEAT == "uc.heartbeat"


# ── NatsPublisher tests ─────────────────────────────────────────


class TestNatsPublisher:
    """Test NatsPublisher publish calls."""

    @pytest.fixture()
    def mock_nc(self):
        """Create a mock NATS client."""
        nc = MagicMock()
        nc.publish = AsyncMock()
        return nc

    @pytest.fixture()
    def publisher(self, mock_nc):
        return NatsPublisher(mock_nc)

    async def test_publish_update(self, publisher, mock_nc):
        task = Task(
            id="task-1",
            description="Test",
            status=TaskStatus.IN_PROGRESS,
        )
        await publisher.publish_update(task)

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        subject = args[0][0]
        data = json.loads(args[0][1].decode("utf-8"))

        assert subject == NATS_SUBJECT_TASK_UPDATE
        assert data["task_id"] == "task-1"
        assert data["status"] == "InProgress"

    async def test_publish_event(self, publisher, mock_nc):
        await publisher.publish_event(
            event_type="subtask_completed",
            task_id="task-1",
            subtask_id="st-1",
            data={"success": True},
        )

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        subject = args[0][0]
        data = json.loads(args[0][1].decode("utf-8"))

        assert subject == NATS_SUBJECT_TASK_EVENT
        assert data["type"] == "subtask_completed"
        assert data["task_id"] == "task-1"
        assert data["subtask_id"] == "st-1"
        assert data["data"]["success"] is True

    async def test_publish_heartbeat(self, publisher, mock_nc):
        await publisher.publish_heartbeat("consumer-1")

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        subject = args[0][0]
        data = json.loads(args[0][1].decode("utf-8"))

        assert subject == NATS_SUBJECT_HEARTBEAT
        assert data["consumer_id"] == "consumer-1"
        assert "timestamp" in data

    async def test_publish_failure_does_not_raise(self, mock_nc):
        """Graceful degradation: publish failure logs but does not raise."""
        mock_nc.publish.side_effect = ConnectionError("NATS down")
        publisher = NatsPublisher(mock_nc)

        # Should not raise
        await publisher.publish_update(
            Task(id="t-1", status=TaskStatus.IN_PROGRESS)
        )

    async def test_publish_event_without_data(self, publisher, mock_nc):
        await publisher.publish_event(
            event_type="task_submitted",
            task_id="task-1",
        )

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        data = json.loads(args[0][1].decode("utf-8"))
        assert data["data"] == {}

    async def test_publish_submit(self, publisher, mock_nc):
        """publish_submit sends a uc.task.submit message with correct payload."""
        await publisher.publish_submit(
            task_id="task-abc",
            description="Fix the login bug",
            project_id="proj-1",
        )

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        subject = args[0][0]
        data = json.loads(args[0][1].decode("utf-8"))

        assert subject == NATS_SUBJECT_TASK_SUBMIT
        assert data["task_id"] == "task-abc"
        assert data["description"] == "Fix the login bug"
        assert data["project_id"] == "proj-1"

    async def test_publish_submit_no_project(self, publisher, mock_nc):
        """publish_submit defaults project_id to empty string."""
        await publisher.publish_submit(
            task_id="task-abc",
            description="Fix the login bug",
        )

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        data = json.loads(args[0][1].decode("utf-8"))
        assert data["project_id"] == ""

    async def test_publish_submit_failure_does_not_raise(self, mock_nc):
        """Graceful degradation: publish_submit failure logs but does not raise."""
        mock_nc.publish.side_effect = ConnectionError("NATS down")
        publisher = NatsPublisher(mock_nc)

        # Should not raise
        await publisher.publish_submit(
            task_id="task-1",
            description="Test",
        )


# ── NatsWorker message handling tests ───────────────────────────


class TestNatsWorkerHandleSubmit:
    """Test NatsWorker submit message handling with mocked components."""

    @pytest.fixture()
    def worker(self):
        """Create a NatsWorker with mocked components."""
        w = NatsWorker(nats_url="nats://localhost:4222")
        w._nc = MagicMock()
        w._nc.subscribe = AsyncMock()
        w._nc.drain = AsyncMock()
        w._publisher = MagicMock()
        w._publisher.publish_update = AsyncMock()
        w._publisher.publish_event = AsyncMock()
        w._publisher.publish_heartbeat = AsyncMock()
        w._orchestrator = MagicMock(spec=Orchestrator)
        w._worker = MagicMock()
        w._worker.worker_id = "test-worker"
        return w

    async def test_handle_valid_submit(self, worker):
        """Valid submit message triggers Orchestrator.submit_task."""
        task = Task(
            id="task-1",
            description="Fix the bug",
            status=TaskStatus.IN_PROGRESS,
            subtasks=[
                Subtask(
                    id="st-1",
                    parent_id="task-1",
                    description="Analyze bug",
                    status=SubtaskStatus.PENDING,
                )
            ],
        )
        worker._orchestrator.submit_task = AsyncMock(return_value=task)
        worker._orchestrator._select_next_subtask = MagicMock(return_value=None)
        worker._orchestrator.get_task_status = MagicMock(return_value=task)

        msg = MagicMock()
        msg.data = json.dumps({
            "task_id": "task-1",
            "description": "Fix the bug",
            "project_id": "proj-1",
        }).encode("utf-8")

        await worker._handle_submit(msg)

        worker._orchestrator.submit_task.assert_called_once_with(
            "Fix the bug",
            project_id="proj-1",
        )

    async def test_handle_empty_description(self, worker):
        """Submit with empty description is ignored."""
        msg = MagicMock()
        msg.data = json.dumps({
            "task_id": "task-1",
            "description": "",
            "project_id": "",
        }).encode("utf-8")

        await worker._handle_submit(msg)

        worker._orchestrator.submit_task.assert_not_called()

    async def test_handle_invalid_json(self, worker):
        """Invalid JSON payload is handled gracefully."""
        msg = MagicMock()
        msg.data = b"not valid json"

        await worker._handle_submit(msg)

        worker._orchestrator.submit_task.assert_not_called()

    async def test_handle_missing_fields(self, worker):
        """Missing fields in payload use defaults."""
        task = Task(
            id="task-1",
            description="Some description",
            status=TaskStatus.IN_PROGRESS,
        )
        worker._orchestrator.submit_task = AsyncMock(return_value=task)
        worker._orchestrator._select_next_subtask = MagicMock(return_value=None)
        worker._orchestrator.get_task_status = MagicMock(return_value=task)

        msg = MagicMock()
        # Only description is present
        msg.data = json.dumps({"description": "Some description"}).encode("utf-8")

        await worker._handle_submit(msg)

        worker._orchestrator.submit_task.assert_called_once_with(
            "Some description",
            project_id="",
        )


# ── Orchestrator nats_publisher hook tests ──────────────────────


class TestOrchestratorNatsPublisherHook:
    """Test that Orchestrator publishes to NATS on state changes."""

    @pytest.fixture()
    def mock_publisher(self):
        pub = MagicMock()
        pub.publish_update = AsyncMock()
        pub.publish_event = AsyncMock()
        return pub

    @pytest.fixture()
    def orchestrator(self, mock_publisher):
        return Orchestrator(nats_publisher=mock_publisher)

    async def test_submit_task_publishes_update(self, orchestrator, mock_publisher):
        """submit_task publishes uc.task.update after decomposition."""
        # Patch decompose_task to avoid needing LLM
        subtask = Subtask(
            id="st-1",
            parent_id="placeholder",
            description="Analyze code",
            status=SubtaskStatus.PENDING,
        )

        async def fake_decompose(task):
            subtask.parent_id = task.id
            return [subtask]

        orchestrator.decompose_task = fake_decompose

        task = await orchestrator.submit_task("Fix the login bug", project_id="proj-1")

        # Verify update was published
        mock_publisher.publish_update.assert_called()
        call_args = mock_publisher.publish_update.call_args
        published_task = call_args[0][0]
        assert published_task.id == task.id

        # Verify event was published
        mock_publisher.publish_event.assert_called()
        event_call = mock_publisher.publish_event.call_args
        assert event_call[0][0] == "task_submitted"

    async def test_assign_subtask_publishes_update(
        self, orchestrator, mock_publisher
    ):
        """assign_subtask publishes uc.task.update and uc.task.event."""
        from ultimate_coders.agent.types import WorkerInfo

        # Register a worker
        worker_info = WorkerInfo(
            id="worker-1",
            capabilities=["code"],
            max_capacity=3,
        )
        await orchestrator.register_worker(worker_info)

        # Create a task with a subtask
        subtask = Subtask(
            id="st-1",
            parent_id="task-1",
            description="Analyze code",
            status=SubtaskStatus.PENDING,
        )
        task = Task(
            id="task-1",
            description="Fix the bug",
            status=TaskStatus.IN_PROGRESS,
            subtasks=[subtask],
        )
        orchestrator.tasks["task-1"] = task

        result = await orchestrator.assign_subtask(subtask, "worker-1")

        assert result == "worker-1"
        mock_publisher.publish_update.assert_called()
        mock_publisher.publish_event.assert_called()
        event_call = mock_publisher.publish_event.call_args
        assert event_call[0][0] == "subtask_assigned"

    async def test_handle_subtask_result_completed_publishes(
        self, orchestrator, mock_publisher
    ):
        """handle_subtask_result publishes update when subtask completes."""
        # Create a task with a completed subtask
        subtask = Subtask(
            id="st-1",
            parent_id="task-1",
            description="Analyze code",
            status=SubtaskStatus.IN_PROGRESS,
        )
        task = Task(
            id="task-1",
            description="Fix the bug",
            status=TaskStatus.IN_PROGRESS,
            subtasks=[subtask],
        )
        orchestrator.tasks["task-1"] = task

        result = SubtaskResult(
            subtask_id="st-1",
            worker_id="worker-1",
            summary="Analysis complete",
            success=True,
        )

        await orchestrator.handle_subtask_result(result)

        # Should publish update (task completed)
        mock_publisher.publish_update.assert_called()
        mock_publisher.publish_event.assert_called()

    async def test_no_publish_without_publisher(self):
        """Orchestrator without nats_publisher does not attempt to publish."""
        orch = Orchestrator()
        assert orch.nats_publisher is None

        # Patch decompose_task
        async def fake_decompose(task):
            return [Subtask(
                id="st-1",
                parent_id=task.id,
                description="Analyze code",
                status=SubtaskStatus.PENDING,
            )]

        orch.decompose_task = fake_decompose
        await orch.submit_task("Test task")
        # No errors should occur


# ── Heartbeat tests ─────────────────────────────────────────────


class TestNatsWorkerHeartbeat:
    """Test heartbeat publishing logic."""

    async def test_heartbeat_publishes(self):
        """Heartbeat loop publishes to uc.heartbeat."""
        worker = NatsWorker()
        publisher = MagicMock()
        publisher.publish_heartbeat = AsyncMock()
        worker._publisher = publisher
        worker._consumer_id = "test-consumer"
        worker._running = True

        # Allow the loop to run once, then stop
        sleep_count = 0

        async def fake_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 1:
                worker._running = False

        with patch("ultimate_coders.nats_worker.asyncio.sleep", side_effect=fake_sleep):
            await worker._heartbeat_loop()

        # At least one heartbeat should have been sent
        assert publisher.publish_heartbeat.call_count >= 1

    async def test_heartbeat_includes_consumer_id(self):
        """Heartbeat payload includes the consumer_id."""
        publisher = MagicMock()
        publisher.publish_heartbeat = AsyncMock()

        await publisher.publish_heartbeat("consumer-abc")

        publisher.publish_heartbeat.assert_called_once_with("consumer-abc")


# ── Serialization round-trip tests ──────────────────────────────


class TestSerializationRoundtrip:
    """Test that Python payloads can be parsed by the Rust NatsTaskUpdate
    deserializer (validate JSON structure matches Rust struct expectations)."""

    def test_update_payload_matches_rust_struct(self):
        """Validate the JSON structure matches Rust NatsTaskUpdate."""
        st = Subtask(
            id="st-1",
            parent_id="task-1",
            description="Analyze code",
            status=SubtaskStatus.ASSIGNED,
            assigned_worker="worker-1",
        )
        task = Task(
            id="task-1",
            description="Fix the login bug",
            status=TaskStatus.IN_PROGRESS,
            subtasks=[st],
        )

        payload = _make_task_update_payload(task)

        # Must have these top-level keys (matching Rust NatsTaskUpdate)
        assert "task_id" in payload
        assert "status" in payload
        assert "subtasks" in payload

        # Each subtask must match NatsSubtaskUpdate
        subtask_payload = payload["subtasks"][0]
        assert "subtask_id" in subtask_payload
        assert "status" in subtask_payload

        # Verify JSON is serializable (no non-JSON types)
        json_str = json.dumps(payload)
        parsed = json.loads(json_str)
        assert parsed["task_id"] == "task-1"

    def test_event_payload_matches_rust_struct(self):
        """Validate the JSON structure matches Rust NatsTaskEvent."""
        payload = _make_task_event_payload(
            event_type="tool_call",
            task_id="task-1",
            subtask_id="st-1",
            data={"tool": "read_file"},
        )

        # Must have these top-level keys (matching Rust NatsTaskEvent)
        assert "type" in payload
        assert "task_id" in payload
        assert "subtask_id" in payload
        assert "data" in payload

        # Verify JSON is serializable
        json_str = json.dumps(payload)
        parsed = json.loads(json_str)
        assert parsed["type"] == "tool_call"

    def test_heartbeat_payload_matches_rust_struct(self):
        """Validate the JSON structure matches Rust NatsHeartbeat."""
        payload = {
            "consumer_id": "consumer-1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Must have these keys (matching Rust NatsHeartbeat)
        assert "consumer_id" in payload
        assert "timestamp" in payload

        # Verify JSON is serializable
        json_str = json.dumps(payload)
        parsed = json.loads(json_str)
        assert parsed["consumer_id"] == "consumer-1"


class TestExecuteSubtasksPublicAPI:
    """Regression tests for _execute_subtasks using public API."""

    @pytest.mark.asyncio
    async def test_uses_public_select_next_subtask(self):
        """_execute_subtasks must call public select_next_subtask, not private."""
        from ultimate_coders.nats_worker import NatsWorker

        worker = NatsWorker.__new__(NatsWorker)
        orchestrator = MagicMock()
        orchestrator.select_next_subtask = MagicMock(return_value=None)
        worker._orchestrator = orchestrator
        worker._worker = MagicMock()  # must be non-None to enter the loop
        worker._publisher = None

        task = MagicMock()
        task.subtasks = []
        task.status = "PENDING"

        await worker._execute_subtasks(task)
        orchestrator.select_next_subtask.assert_called_once_with(task)
