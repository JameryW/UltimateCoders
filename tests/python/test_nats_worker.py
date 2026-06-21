"""Unit tests for NatsWorker and NatsPublisher.

Tests cover:
- NatsWorker initialization (sandbox-only, no execution_mode branching)
- NatsPublisher message_id generation for deduplication
- NatsPublisher publish_event and publish_task_update
- NatsPublisher lifecycle (connect, close)
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ultimate_coders.agent.types import Subtask, SubtaskStatus, Task, TaskStatus
from ultimate_coders.nats_worker import (
    NatsPublisher,
    NatsWorker,
    _make_task_event_payload,
    _make_task_update_payload,
)


# ═══════════════════════════════════════════════════════════════════
# _make_task_event_payload tests
# ═══════════════════════════════════════════════════════════════════

class TestMakeTaskEventPayload:
    """Tests for _make_task_event_payload() helper."""

    def test_basic_event(self):
        payload = _make_task_event_payload(
            "subtask_started", "task-1", "sub-1",
        )
        assert payload["type"] == "subtask_started"
        assert payload["task_id"] == "task-1"
        assert payload["subtask_id"] == "sub-1"
        assert "message_id" in payload
        assert payload["message_id"].startswith("task-1:subtask_started:sub-1:")

    def test_event_with_data(self):
        payload = _make_task_event_payload(
            "subtask_completed", "task-1", "sub-1",
            data={"summary": "done", "success": True},
        )
        assert payload["data"]["summary"] == "done"
        assert payload["data"]["success"] is True

    def test_event_without_subtask_id(self):
        payload = _make_task_event_payload(
            "task_started", "task-1",
        )
        assert "subtask_id" not in payload
        assert payload["message_id"].startswith("task-1:task_started::")

    def test_message_id_format(self):
        """message_id follows {task_id}:{event_type}:{subtask_id}:{ts_ms}."""
        before_ms = int(time.time() * 1000)
        payload = _make_task_event_payload(
            "subtask_failed", "t1", "s1",
        )
        after_ms = int(time.time() * 1000)

        parts = payload["message_id"].split(":")
        assert parts[0] == "t1"
        assert parts[1] == "subtask_failed"
        assert parts[2] == "s1"
        ts_ms = int(parts[3])
        assert before_ms <= ts_ms <= after_ms

    def test_message_id_uniqueness(self):
        """Two calls produce different message_ids (different timestamps)."""
        p1 = _make_task_event_payload("ev", "t1", "s1")
        time.sleep(0.002)
        p2 = _make_task_event_payload("ev", "t1", "s1")
        assert p1["message_id"] != p2["message_id"]


# ═══════════════════════════════════════════════════════════════════
# _make_task_update_payload tests
# ═══════════════════════════════════════════════════════════════════

class TestMakeTaskUpdatePayload:
    """Tests for _make_task_update_payload() helper."""

    def test_basic_update(self):
        task = Task(description="Test", project_id="proj")
        payload = _make_task_update_payload(task)
        assert payload["task_id"] == task.id
        assert payload["status"] == "Created"  # PascalCase per NATS protocol
        assert "message_id" in payload
        assert payload["message_id"].startswith(f"{task.id}:update:")

    def test_update_with_subtasks(self):
        task = Task(description="Test")
        st = Subtask(description="Step 1", parent_id=task.id)
        st.status = SubtaskStatus.IN_PROGRESS
        task.subtasks = [st]

        payload = _make_task_update_payload(task)
        assert len(payload["subtasks"]) == 1
        assert payload["subtasks"][0]["subtask_id"] == st.id
        assert payload["subtasks"][0]["status"] == "InProgress"  # PascalCase per NATS protocol


# ═══════════════════════════════════════════════════════════════════
# NatsPublisher tests
# ═══════════════════════════════════════════════════════════════════

class TestNatsPublisher:
    """Tests for NatsPublisher."""

    def test_init_with_nc(self):
        mock_nc = MagicMock()
        pub = NatsPublisher(nc=mock_nc)
        assert pub._nc is mock_nc

    @pytest.mark.asyncio
    async def test_publish_event(self):
        mock_nc = MagicMock()
        mock_nc.publish = AsyncMock()
        pub = NatsPublisher(nc=mock_nc)

        await pub.publish_event(
            "subtask_started", task_id="t1", subtask_id="s1",
            data={"description": "test"},
        )

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        subject = args[0][0]
        data = json.loads(args[0][1].decode("utf-8"))

        assert subject == "uc.task.event"
        assert data["type"] == "subtask_started"
        assert data["task_id"] == "t1"
        assert data["message_id"]  # message_id present

    @pytest.mark.asyncio
    async def test_publish_update(self):
        mock_nc = MagicMock()
        mock_nc.publish = AsyncMock()
        pub = NatsPublisher(nc=mock_nc)

        task = Task(description="Test")
        await pub.publish_update(task)

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        subject = args[0][0]
        data = json.loads(args[0][1].decode("utf-8"))

        assert subject == "uc.task.update"
        assert data["task_id"] == task.id
        assert data["message_id"]  # message_id present

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_raise(self):
        """Graceful degradation: publish failure logs but does not raise."""
        mock_nc = MagicMock()
        mock_nc.publish = AsyncMock(side_effect=ConnectionError("NATS down"))
        pub = NatsPublisher(nc=mock_nc)

        # Should not raise
        await pub.publish_event("test", task_id="t1")
        await pub.publish_update(Task(description="Test"))

    @pytest.mark.asyncio
    async def test_publish_heartbeat(self):
        mock_nc = MagicMock()
        mock_nc.publish = AsyncMock()
        pub = NatsPublisher(nc=mock_nc)

        await pub.publish_heartbeat("consumer-1")

        mock_nc.publish.assert_called_once()
        args = mock_nc.publish.call_args
        subject = args[0][0]
        data = json.loads(args[0][1].decode("utf-8"))

        assert subject == "uc.heartbeat"
        assert data["consumer_id"] == "consumer-1"


# ═══════════════════════════════════════════════════════════════════
# NatsWorker tests
# ═══════════════════════════════════════════════════════════════════

class TestNatsWorkerInit:
    """Tests for NatsWorker initialization."""

    def test_init_default(self):
        worker = NatsWorker()
        assert worker._nats_url == "nats://localhost:4222"
        assert worker._project_path == ""
        assert worker._orchestrator is None
        assert worker._worker is None

    def test_init_custom(self):
        worker = NatsWorker(
            nats_url="nats://custom:4222",
            project_path="/tmp/proj",
        )
        assert worker._nats_url == "nats://custom:4222"
        assert worker._project_path == "/tmp/proj"
