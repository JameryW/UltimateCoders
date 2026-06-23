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
    ExecutionSpec,
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

        async def fake_execute(prompt: str, **_kw) -> AgentOutput:
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

        async def fake_execute(prompt: str, **_kw) -> AgentOutput:
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

        async def slow_execute(prompt: str, **_kw) -> AgentOutput:
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

        async def failing_execute(prompt: str, **_kw) -> AgentOutput:
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
        assert orch.llm_client is None
        assert orch.codegraph_client is None
        assert orch.event_emitter is not None

    def test_init_with_sandbox_manager(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(sandbox_manager=sm)
        assert orch.sandbox_manager is sm

    def test_init_with_llm_client(self):
        class FakeLLM:
            pass
        orch = Orchestrator(llm_client=FakeLLM())
        assert orch.llm_client is not None

    def test_init_with_codegraph(self):
        class FakeCodegraph:
            def is_available(self):
                return True
        orch = Orchestrator(codegraph_client=FakeCodegraph())
        assert orch.codegraph_client is not None


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
    """Tests for _gather_memory_context / _gather_code_context."""

    @pytest.mark.asyncio
    async def test_gather_memory_context_no_engine(self):
        orch = Orchestrator()
        task = Task(description="Test", project_id="my-project")
        result = await orch._gather_memory_context(task)
        assert result == ""

    @pytest.mark.asyncio
    async def test_gather_memory_context_with_engine(self, monkeypatch):
        """Memory context reads from engine when available."""
        class FakeEngine:
            def read_memory(self, **kw):
                return "stored task context"
        orch = Orchestrator(engine=FakeEngine())
        task = Task(description="Test", project_id="my-project")
        result = await orch._gather_memory_context(task)
        assert "stored task context" in result

    @pytest.mark.asyncio
    async def test_gather_code_context_no_engine(self):
        orch = Orchestrator()
        task = Task(description="Test")
        result = await orch._gather_code_context(task)
        assert result == ""

    @pytest.mark.asyncio
    async def test_gather_code_context_with_engine_search(self, monkeypatch):
        """Code context uses engine.search_code when available."""
        class FakeResult:
            file_path = "src/main.py"
            content_snippet = "def main():"
            score = 0.9
        class FakeEngine:
            def search_code(self, **kw):
                return [FakeResult()]
        orch = Orchestrator(engine=FakeEngine())
        task = Task(description="Test main function")
        result = await orch._gather_code_context(task)
        assert "src/main.py" in result


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
# Orchestrator Agent Capability tests
# ═══════════════════════════════════════════════════════════════════

class TestOrchestratorTools:
    """Tests for Orchestrator tool definitions and execution."""

    def test_build_tools_no_backends(self):
        """No tools when engine and codegraph are unavailable."""
        orch = Orchestrator()
        tools = orch._build_tools()
        assert tools == []

    def test_build_tools_with_engine(self):
        """Tools include search_code, search_memory, read_file when engine has search."""
        class FakeEngine:
            def search_code(self, **kw):
                return []
        orch = Orchestrator(engine=FakeEngine())
        tools = orch._build_tools()
        names = [t.name for t in tools]
        assert "search_code" in names
        assert "search_memory" in names
        assert "read_file" in names

    def test_build_tools_with_codegraph(self):
        """Tools include codegraph_explore when codegraph is available."""
        class FakeCodegraph:
            def is_available(self):
                return True
        orch = Orchestrator(codegraph_client=FakeCodegraph())
        tools = orch._build_tools()
        names = [t.name for t in tools]
        assert "codegraph_explore" in names

    @pytest.mark.asyncio
    async def test_execute_tool_search_code(self):
        """search_code tool returns formatted results."""
        class FakeResult:
            file_path = "src/main.py"
            content_snippet = "def main():"
            score = 0.95
        class FakeEngine:
            def search_code(self, **kw):
                return [FakeResult()]
        orch = Orchestrator(engine=FakeEngine())

        from ultimate_coders.agent.llm import ToolCall
        tc = ToolCall(id="1", name="search_code", input={"query": "main"})
        result = await orch._execute_tool(tc)
        assert "src/main.py" in result
        assert "0.95" in result

    @pytest.mark.asyncio
    async def test_execute_tool_search_memory(self):
        """search_memory tool reads from engine."""
        class FakeEngine:
            def read_memory(self, **kw):
                return "stored context"
        orch = Orchestrator(engine=FakeEngine())

        from ultimate_coders.agent.llm import ToolCall
        tc = ToolCall(id="2", name="search_memory", input={"key": "task"})
        result = await orch._execute_tool(tc)
        assert "stored context" in result

    @pytest.mark.asyncio
    async def test_execute_tool_codegraph_explore(self):
        """codegraph_explore tool returns explore results."""
        class FakeCodegraph:
            def is_available(self):
                return True
            def explore(self, query, max_nodes=10):
                return "## Symbols\n- main (function)"
        orch = Orchestrator(codegraph_client=FakeCodegraph())

        from ultimate_coders.agent.llm import ToolCall
        tc = ToolCall(id="3", name="codegraph_explore", input={"query": "main"})
        result = await orch._execute_tool(tc)
        assert "main" in result

    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self):
        """Unknown tool returns error message."""
        orch = Orchestrator()
        from ultimate_coders.agent.llm import ToolCall
        tc = ToolCall(id="4", name="nonexistent", input={})
        result = await orch._execute_tool(tc)
        assert "Unknown tool" in result

    def test_truncate(self):
        """_truncate limits text length."""
        orch = Orchestrator()
        long_text = "x" * 5000
        result = orch._truncate(long_text, 100)
        assert len(result) <= 103  # 100 + "..."
        assert result.endswith("...")


class TestOrchestratorPlanTask:
    """Tests for Orchestrator.plan_task()."""

    @pytest.mark.asyncio
    async def test_plan_task_no_llm_fallback(self):
        """plan_task falls back to direct context gathering without LLM."""
        class FakeResult:
            file_path = "src/main.py"
            content_snippet = "def main():"
            score = 0.9
        class FakeEngine:
            def search_code(self, **kw):
                return [FakeResult()]
        orch = Orchestrator(engine=FakeEngine())
        task = Task(description="Fix main function", project_id="proj")

        spec = await orch.plan_task(task)
        assert "src/main.py" in spec.context or "src/main.py" in spec.raw_text

    @pytest.mark.asyncio
    async def test_plan_task_with_llm(self, monkeypatch):
        """plan_task uses LLM tool-calling when available."""
        from ultimate_coders.agent.llm import LLMResponse

        class FakeEngine:
            def search_code(self, **kw):
                return []

        class FakeLLM:
            async def complete_with_tools(self, **kw):
                response = LLMResponse(text=(
                    "## Context\nFound relevant code in src/main.py\n\n"
                    "## Approach\n1. Fix main function\n\n"
                    "## Verification\nRun tests"
                ))
                tool_log = []
                return response, tool_log

        orch = Orchestrator(engine=FakeEngine(), llm_client=FakeLLM())
        task = Task(description="Fix main function")

        spec = await orch.plan_task(task)
        assert isinstance(spec, ExecutionSpec)
        assert "src/main.py" in spec.raw_text or "Fix main" in spec.raw_text

    @pytest.mark.asyncio
    async def test_plan_task_no_backends(self):
        """plan_task returns empty ExecutionSpec with no backends or LLM."""
        orch = Orchestrator()
        task = Task(description="Test")
        spec = await orch.plan_task(task)
        assert isinstance(spec, ExecutionSpec)
        assert spec.raw_text == ""


class TestOrchestratorAsk:
    """Tests for Orchestrator.ask()."""

    @pytest.mark.asyncio
    async def test_ask_no_llm(self):
        """ask returns message when LLM is not configured."""
        orch = Orchestrator()
        result = await orch.ask("What does main.py do?")
        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_ask_with_llm(self, monkeypatch):
        """ask uses LLM to answer questions."""
        from ultimate_coders.agent.llm import LLMResponse

        class FakeEngine:
            def search_code(self, **kw):
                return []

        class FakeLLM:
            async def complete_with_tools(self, **kw):
                return LLMResponse(text="main.py contains the entry point"), []
            async def complete(self, **kw):
                return LLMResponse(text="main.py contains the entry point")

        orch = Orchestrator(engine=FakeEngine(), llm_client=FakeLLM())
        result = await orch.ask("What does main.py do?")
        assert "main.py" in result

    @pytest.mark.asyncio
    async def test_ask_no_tools_direct_completion(self, monkeypatch):
        """ask falls back to direct completion without tools."""
        from ultimate_coders.agent.llm import LLMResponse

        class FakeLLM:
            async def complete(self, **kw):
                return LLMResponse(text="General answer")
            async def complete_with_tools(self, **kw):
                return LLMResponse(text="Tool answer"), []

        # No engine → no tools → should use complete()
        orch = Orchestrator(llm_client=FakeLLM())
        result = await orch.ask("What is 2+2?")
        assert result == "General answer"


class TestOrchestratorDecomposeWithContext:
    """Tests for enhanced decompose_task with planning context."""

    @pytest.mark.asyncio
    async def test_decompose_with_llm_context(self, monkeypatch):
        """decompose_task injects planning context when LLM is available."""
        from ultimate_coders.agent.llm import LLMResponse
        from ultimate_coders.agent.sandbox import ExecResult, SandboxConfig, SandboxManager

        captured_prompt = ""

        class FakeLLM:
            async def complete_with_tools(self, **kw):
                return LLMResponse(text="Relevant: src/main.py has main()"), []
            async def complete(self, **kw):
                return LLMResponse(text="Summary")

        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(sandbox_manager=sm, llm_client=FakeLLM())

        fake_json = '[{"description": "Step 1", "depends_on": []}]'
        fake_result = ExecResult(exit_code=0, stdout=fake_json)

        async def fake_execute_subprocess(request):
            nonlocal captured_prompt
            captured_prompt = request.prompt if hasattr(request, 'prompt') else ""
            return fake_result

        monkeypatch.setattr(sm, "_execute_subprocess", fake_execute_subprocess)

        task = Task(description="Fix main function", project_id="proj")
        subtasks = await orch.decompose_task(task)

        # The prompt should contain the planning context
        assert len(subtasks) == 1
        assert subtasks[0].description == "Step 1"

    @pytest.mark.asyncio
    async def test_decompose_without_llm_still_works(self, monkeypatch):
        """decompose_task still works without LLM (backward compat)."""
        from ultimate_coders.agent.sandbox import ExecResult, SandboxConfig, SandboxManager

        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(sandbox_manager=sm)

        fake_json = '[{"description": "Step 1", "depends_on": []}]'
        fake_result = ExecResult(exit_code=0, stdout=fake_json)

        async def fake_execute_subprocess(request):
            return fake_result

        monkeypatch.setattr(sm, "_execute_subprocess", fake_execute_subprocess)

        task = Task(description="Test task", project_id="proj")
        subtasks = await orch.decompose_task(task)
        assert len(subtasks) == 1




# ═══════════════════════════════════════════════════════════════════
# Agent Loop + Compaction + ExecutionSpec tests
# ═══════════════════════════════════════════════════════════════════

class TestAgentLoop:
    """Tests for Orchestrator._agent_loop()."""

    @pytest.mark.asyncio
    async def test_agent_loop_basic(self):
        """Agent loop returns events and response."""
        from ultimate_coders.agent.llm import LLMResponse
        from ultimate_coders.agent.types import AgentEventType

        class FakeLLM:
            async def complete_with_tools(self, **kw):
                return LLMResponse(text="Done"), []

        orch = Orchestrator(llm_client=FakeLLM())
        from ultimate_coders.agent.llm import make_tool_definition
        tools = [make_tool_definition("test", "A test tool", {})]

        response, tool_log, events = await orch._agent_loop(
            messages=[{"role": "user", "content": "Hello"}],
            tools=tools,
            system="Test",
        )
        assert response.text == "Done"
        assert any(e.type == AgentEventType.AGENT_START for e in events)
        assert any(e.type == AgentEventType.AGENT_END for e in events)

    @pytest.mark.asyncio
    async def test_agent_loop_no_tools(self):
        """Agent loop with no tools uses direct completion."""
        from ultimate_coders.agent.llm import LLMResponse

        class FakeLLM:
            async def complete(self, **kw):
                return LLMResponse(text="Direct answer")

        orch = Orchestrator(llm_client=FakeLLM())
        response, tool_log, events = await orch._agent_loop(
            messages=[{"role": "user", "content": "Hello"}],
            tools=None,
            system="Test",
        )
        assert response.text == "Direct answer"

    @pytest.mark.asyncio
    async def test_agent_loop_abort(self):
        """Agent loop stops when abort_event is set."""
        from ultimate_coders.agent.llm import LLMResponse
        from ultimate_coders.agent.types import AgentEventType, AgentRunConfig

        call_count = 0

        class FakeLLM:
            async def complete_with_tools(self, **kw):
                nonlocal call_count
                call_count += 1
                return LLMResponse(text=f"Turn {call_count}"), []

        orch = Orchestrator(llm_client=FakeLLM())
        from ultimate_coders.agent.llm import make_tool_definition
        tools = [make_tool_definition("test", "A test tool", {})]

        abort = asyncio.Event()
        abort.set()  # Already aborted

        config = AgentRunConfig(max_turns=5, abort_event=abort)
        response, tool_log, events = await orch._agent_loop(
            messages=[{"role": "user", "content": "Hello"}],
            tools=tools,
            system="Test",
            run_config=config,
        )
        # Should have AGENT_START + AGENT_END with abort reason
        assert any(
            e.type == AgentEventType.AGENT_END and e.data.get("reason") == "abort"
            for e in events
        )
        assert call_count == 0  # Never called LLM

    @pytest.mark.asyncio
    async def test_agent_loop_steering(self):
        """Agent loop drains steering messages."""
        from ultimate_coders.agent.llm import LLMResponse
        from ultimate_coders.agent.types import AgentRunConfig

        class FakeLLM:
            async def complete_with_tools(self, **kw):
                return LLMResponse(text="Done"), []

        orch = Orchestrator(llm_client=FakeLLM())
        from ultimate_coders.agent.llm import make_tool_definition
        tools = [make_tool_definition("test", "A test tool", {})]

        queue = asyncio.Queue()
        await queue.put({"role": "user", "content": "Additional context"})

        config = AgentRunConfig(max_turns=5, steering_queue=queue)
        response, tool_log, events = await orch._agent_loop(
            messages=[{"role": "user", "content": "Hello"}],
            tools=tools,
            system="Test",
            run_config=config,
        )
        # Steering message was consumed
        assert queue.empty()


class TestCompaction:
    """Tests for context compaction."""

    @pytest.mark.asyncio
    async def test_compact_context_preserves_recent(self):
        """Compaction keeps recent messages intact."""
        from ultimate_coders.agent.llm import LLMResponse

        class FakeLLM:
            async def complete(self, **kw):
                return LLMResponse(text="Summary of old context")

        orch = Orchestrator(llm_client=FakeLLM())
        messages = [
            {"role": "user", "content": "Old message 1"},
            {"role": "assistant", "content": "Old response"},
            {"role": "user", "content": "Recent message"},
            {"role": "assistant", "content": "Recent response"},
        ]
        compacted = await orch._compact_context(messages, keep_recent=2)
        # Last 2 messages preserved
        assert compacted[-1]["content"] == "Recent response"
        assert compacted[-2]["content"] == "Recent message"

    @pytest.mark.asyncio
    async def test_compact_context_no_llm(self):
        """Without LLM, compaction drops old messages."""
        orch = Orchestrator()  # No LLM
        messages = [
            {"role": "user", "content": "Old"},
            {"role": "assistant", "content": "Response"},
            {"role": "user", "content": "Recent"},
        ]
        compacted = await orch._compact_context(messages, keep_recent=1)
        assert len(compacted) == 1
        assert compacted[0]["content"] == "Recent"

    def test_estimate_tokens(self):
        """Token estimation is reasonable."""
        orch = Orchestrator()
        messages = [
            {"role": "user", "content": "x" * 400},  # ~100 tokens
        ]
        assert orch._estimate_tokens(messages) == 100


class TestExecutionSpec:
    """Tests for ExecutionSpec parsing and structure."""

    def test_parse_execution_spec(self):
        """Parse LLM output into ExecutionSpec."""
        orch = Orchestrator()
        text = (
            "## Context\nThe project uses Python.\n\n"
            "## Approach\n1. Read config file\n2. Update settings\n\n"
            "## Critical Files & Anchors\n- config.py:Settings\n\n"
            "## Verification\nRun tests\n\n"
            "## Assumptions & Contingencies\nIf config missing, create it"
        )
        spec = orch._parse_execution_spec(text)
        assert "Python" in spec.context
        assert len(spec.approach) == 2
        assert "config.py" in spec.critical_files[0]
        assert "tests" in spec.verification
        assert "missing" in spec.assumptions
        assert spec.raw_text == text

    def test_parse_execution_spec_fallback(self):
        """Unstructured text falls back to raw_text."""
        orch = Orchestrator()
        spec = orch._parse_execution_spec("Just some text without headers")
        assert spec.raw_text == "Just some text without headers"
        assert spec.context == ""
        assert spec.approach == []

    def test_orchestrate_notice_in_plan_prompt(self):
        """The orchestrate notice is included in plan_task system prompt."""
        assert "ORCHESTRATE RULES" in Orchestrator._ORCHESTRATE_NOTICE
        assert "Decompose" in Orchestrator._ORCHESTRATE_NOTICE
        assert "Parallelize" in Orchestrator._ORCHESTRATE_NOTICE



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
