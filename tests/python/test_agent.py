"""Unit tests for Agent layer (Worker, Orchestrator, types).

Worker tests cover sandbox-only execution, conflict detection,
heartbeat, and event publishing.

Orchestrator tests cover task decomposition (sandbox path),
_parse_decomposition_items, worker assignment, and task lifecycle.

Previously contained LLM tool-calling tests — removed in favor of
sandbox-only execution.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictResult,
)
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.sandbox import (
    AgentOutput,
    ExecResult,
    SandboxConfig,
    SandboxManager,
)
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkerInfo,
)
from ultimate_coders.agent.worker import Worker

# ═══════════════════════════════════════════════════════════════════
# Worker tests
# ═══════════════════════════════════════════════════════════════════

class TestWorkerInit:
    """Tests for Worker initialization."""

    def test_default_init(self):
        worker = Worker()
        assert worker.worker_id
        assert worker.engine is None
        assert worker.capabilities == ["code", "search", "memory", "test"]
        assert worker.max_capacity == 3
        assert worker.current_task is None
        assert worker._active_count == 0
        assert worker.nats_publisher is None
        assert worker.event_emitter is None

    def test_custom_init(self):
        config = SandboxConfig(project_path="/tmp/test")
        worker = Worker(
            worker_id="w1",
            engine=None,
            capabilities=["code"],
            max_capacity=5,
            sandbox_config=config,
        )
        assert worker.worker_id == "w1"
        assert worker.capabilities == ["code"]
        assert worker.max_capacity == 5
        assert worker._sandbox_config is config

    def test_get_info(self):
        worker = Worker(worker_id="w1", max_capacity=5)
        info = worker.get_info()
        assert isinstance(info, WorkerInfo)
        assert info.id == "w1"
        assert info.max_capacity == 5


class TestWorkerSandboxExecution:
    """Tests for Worker sandbox-only execution."""

    @pytest.mark.asyncio
    async def test_execute_subtask_success(self, monkeypatch):
        """Worker executes subtask via sandbox and returns result."""
        worker = Worker()

        fake_output = AgentOutput(
            success=True,
            summary="Task completed",
            file_changes=[],
        )

        async def fake_execute(prompt: str) -> AgentOutput:
            return fake_output

        monkeypatch.setattr(worker._sandbox_manager, "execute", fake_execute)

        subtask = Subtask(
            description="Fix the bug",
            parent_id="task-1",
        )
        result = await worker.execute_subtask(subtask)

        assert result.success
        assert result.summary == "Task completed"
        assert result.subtask_id == subtask.id
        assert result.worker_id == worker.worker_id

    @pytest.mark.asyncio
    async def test_execute_subtask_failure(self, monkeypatch):
        """Worker handles sandbox execution failure."""
        worker = Worker()

        fake_output = AgentOutput(
            success=False,
            summary="Sandbox error",
            file_changes=[],
        )

        async def fake_execute(prompt: str) -> AgentOutput:
            return fake_output

        monkeypatch.setattr(worker._sandbox_manager, "execute", fake_execute)

        subtask = Subtask(
            description="Fix the bug",
            parent_id="task-1",
        )
        result = await worker.execute_subtask(subtask)

        assert not result.success
        assert result.summary == "Sandbox error"

    @pytest.mark.asyncio
    async def test_execute_subtask_timeout(self, monkeypatch):
        """Worker times out if sandbox takes too long."""

        async def slow_execute(prompt: str) -> AgentOutput:
            await asyncio.sleep(10)
            return AgentOutput(success=True, summary="done", file_changes=[])

        worker = Worker()
        monkeypatch.setattr(worker._sandbox_manager, "execute", slow_execute)

        subtask = Subtask(
            description="Fix the bug",
            parent_id="task-1",
            timeout_seconds=1,
        )
        result = await worker.execute_subtask(subtask)

        assert not result.success
        assert "timed out" in result.summary

    @pytest.mark.asyncio
    async def test_execute_subtask_exception(self, monkeypatch):
        """Worker handles unexpected exceptions during execution."""

        async def failing_execute(prompt: str) -> AgentOutput:
            raise RuntimeError("Unexpected error")

        worker = Worker()
        monkeypatch.setattr(worker._sandbox_manager, "execute", failing_execute)

        subtask = Subtask(
            description="Fix the bug",
            parent_id="task-1",
        )
        result = await worker.execute_subtask(subtask)

        assert not result.success
        assert "Unexpected error" in result.summary


class TestWorkerEventPublishing:
    """Tests for Worker event publishing (NATS preferred, event_emitter fallback)."""

    @pytest.mark.asyncio
    async def test_publish_via_nats(self):
        """Worker publishes events via NATS when available."""
        mock_nats = AsyncMock()
        worker = Worker(nats_publisher=mock_nats)

        subtask = Subtask(description="Test", parent_id="task-1")

        # Mock sandbox to succeed immediately
        fake_output = AgentOutput(success=True, summary="done", file_changes=[])
        with patch.object(worker._sandbox_manager, "execute", return_value=fake_output):
            await worker.execute_subtask(subtask)

        # NATS publisher should have been called
        assert mock_nats.publish_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_publish_via_event_emitter_fallback(self):
        """Worker falls back to event_emitter when NATS is not available."""
        mock_emitter = AsyncMock()
        worker = Worker(event_emitter=mock_emitter)

        subtask = Subtask(description="Test", parent_id="task-1")

        fake_output = AgentOutput(success=True, summary="done", file_changes=[])
        with patch.object(worker._sandbox_manager, "execute", return_value=fake_output):
            await worker.execute_subtask(subtask)

        # Event emitter should have been called
        assert mock_emitter.emit.call_count >= 1


class TestWorkerConflictDetection:
    """Tests for Worker conflict detection."""

    def test_declare_edit_intent_no_conflict(self):
        worker = Worker(worker_id="w1")
        result, info = worker.declare_edit_intent("src/main.py")
        assert result == ConflictResult.NO_CONFLICT
        assert info is None

    def test_declare_edit_intent_with_conflict(self):
        detector = ConflictDetector()
        worker1 = Worker(worker_id="w1", conflict_detector=detector)
        worker2 = Worker(worker_id="w2", conflict_detector=detector)

        worker1.declare_edit_intent("src/main.py")
        result, info = worker2.declare_edit_intent("src/main.py")
        assert result != ConflictResult.NO_CONFLICT

    def test_release_edit_intent(self):
        detector = ConflictDetector()
        worker = Worker(worker_id="w1", conflict_detector=detector)
        worker.declare_edit_intent("src/main.py")
        worker.release_edit_intent("src/main.py")
        # Should be able to declare again without conflict
        result, _ = worker.declare_edit_intent("src/main.py")
        assert result == ConflictResult.NO_CONFLICT


class TestWorkerHeartbeat:
    """Tests for Worker heartbeat."""

    @pytest.mark.asyncio
    async def test_heartbeat(self):
        worker = Worker(worker_id="w1", max_capacity=5)
        hb = await worker.send_heartbeat()
        assert hb["worker_id"] == "w1"
        assert hb["max_capacity"] == 5
        assert hb["current_load"] == 0


# ═══════════════════════════════════════════════════════════════════
# Orchestrator tests
# ═══════════════════════════════════════════════════════════════════

class TestOrchestratorInit:
    """Tests for Orchestrator initialization."""

    def test_default_init(self):
        orch = Orchestrator()
        assert orch.engine is None
        assert orch.sandbox_manager is None
        assert orch.nats_publisher is None
        assert orch.event_emitter is not None

    def test_init_with_sandbox_manager(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(sandbox_manager=sm)
        assert orch.sandbox_manager is sm


class TestOrchestratorDecompose:
    """Tests for Orchestrator task decomposition (sandbox-only)."""

    @pytest.mark.asyncio
    async def test_decompose_requires_sandbox_manager(self):
        """Decomposition without sandbox_manager raises RuntimeError."""
        orch = Orchestrator()
        task = Task(description="Test task")

        with pytest.raises(RuntimeError, match="sandbox_manager is required"):
            await orch.decompose_task(task)

    @pytest.mark.asyncio
    async def test_decompose_sandbox_success(self, monkeypatch):
        """Sandbox decomposition returns subtasks."""
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(sandbox_manager=sm)

        fake_json = '[{"description": "Step 1", "depends_on": []}]'
        fake_result = ExecResult(exit_code=0, stdout=fake_json)

        async def fake_execute_subprocess(request):
            return fake_result

        monkeypatch.setattr(sm, "_execute_subprocess", fake_execute_subprocess)

        task = Task(description="Test task", project_id="test")
        subtasks = await orch.decompose_task(task)
        assert len(subtasks) == 1
        assert subtasks[0].description == "Step 1"


class TestOrchestratorParseItems:
    """Tests for Orchestrator._parse_decomposition_items()."""

    def test_basic_items(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(sandbox_manager=sm)

        items = [
            {"description": "Task A", "depends_on": []},
            {"description": "Task B", "depends_on": [0]},
        ]
        subtasks = orch._parse_decomposition_items(items, "p1")
        assert len(subtasks) == 2
        assert subtasks[0].description == "Task A"
        assert subtasks[1].depends_on == [subtasks[0].id]

    def test_empty_items(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(sandbox_manager=sm)
        assert orch._parse_decomposition_items([], "p1") == []


class TestOrchestratorGatherContext:
    """Tests for simplified _gather_memory_context / _gather_code_context."""

    @pytest.mark.asyncio
    async def test_gather_memory_context_returns_project_id(self):
        orch = Orchestrator()
        task = Task(description="Test", project_id="my-project")
        result = await orch._gather_memory_context(task)
        assert result == "my-project"

    @pytest.mark.asyncio
    async def test_gather_memory_context_empty_when_no_project(self):
        orch = Orchestrator()
        task = Task(description="Test")
        result = await orch._gather_memory_context(task)
        assert result == ""

    @pytest.mark.asyncio
    async def test_gather_code_context_returns_project_id(self):
        orch = Orchestrator()
        task = Task(description="Test", project_id="my-project")
        result = await orch._gather_code_context(task)
        assert result == "my-project"


class TestOrchestratorWorkerRegistration:
    """Tests for Orchestrator worker management."""

    @pytest.mark.asyncio
    async def test_register_worker(self):
        orch = Orchestrator()
        info = WorkerInfo(id="w1", capabilities=["code"], current_load=0, max_capacity=3)
        await orch.register_worker(info)
        assert "w1" in orch.workers

    @pytest.mark.asyncio
    async def test_unregister_worker(self):
        orch = Orchestrator()
        info = WorkerInfo(id="w1", capabilities=["code"], current_load=0, max_capacity=3)
        await orch.register_worker(info)
        await orch.unregister_worker("w1")
        assert "w1" not in orch.workers


# ═══════════════════════════════════════════════════════════════════
# Type tests
# ═══════════════════════════════════════════════════════════════════

class TestSubtaskTypes:
    """Tests for Subtask type."""

    def test_subtask_creation(self):
        st = Subtask(description="Test", parent_id="p1")
        assert st.description == "Test"
        assert st.parent_id == "p1"
        assert st.status == SubtaskStatus.PENDING

    def test_subtask_status_checks(self):
        st = Subtask(description="Test", parent_id="p1")
        st.status = SubtaskStatus.IN_PROGRESS
        assert not st.is_ready
        assert not st.is_complete


class TestTaskTypes:
    """Tests for Task type."""

    def test_task_creation(self):
        task = Task(description="Test task", project_id="proj1")
        assert task.description == "Test task"
        assert task.project_id == "proj1"
        assert task.status == TaskStatus.CREATED

    def test_task_with_subtasks(self):
        task = Task(description="Test task")
        st1 = Subtask(description="Step 1", parent_id=task.id)
        st2 = Subtask(description="Step 2", parent_id=task.id, depends_on=[st1.id])
        task.subtasks = [st1, st2]
        assert len(task.subtasks) == 2
