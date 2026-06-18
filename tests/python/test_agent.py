"""Unit tests for the agent layer — Orchestrator, Worker, types, and LLM client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from ultimate_coders.agent.llm import (
    LLMClient,
    LLMResponse,
    ToolCall,
    make_tool_definition,
)
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkerInfo,
)
from ultimate_coders.agent.worker import Worker
from ultimate_coders.memory.memory import LongTermMemory, MemoryEntry, MemoryKey, ShortTermMemory

# ── Types tests ──────────────────────────────────────────────────


class TestTask:
    """Tests for Task dataclass."""

    def test_task_defaults(self):
        task = Task()
        assert task.id  # auto-generated UUID
        assert task.status == TaskStatus.CREATED
        assert task.subtasks == []
        assert task.description == ""
        assert task.project_id == ""

    def test_task_is_complete(self):
        task = Task()
        assert not task.is_complete  # No subtasks

        st1 = Subtask(parent_id=task.id, status=SubtaskStatus.COMPLETED)
        st2 = Subtask(parent_id=task.id, status=SubtaskStatus.COMPLETED)
        task.subtasks = [st1, st2]
        assert task.is_complete

    def test_task_has_failed(self):
        task = Task()
        st = Subtask(parent_id=task.id, status=SubtaskStatus.FAILED)
        task.subtasks = [st]
        assert task.has_failed

    def test_task_ready_subtasks(self):
        task = Task()
        st1 = Subtask(parent_id=task.id, status=SubtaskStatus.PENDING)
        st2 = Subtask(parent_id=task.id, status=SubtaskStatus.PENDING, depends_on=[st1.id])
        task.subtasks = [st1, st2]

        ready = task.ready_subtasks
        assert len(ready) == 1
        assert ready[0].id == st1.id

    def test_task_ready_subtasks_with_completed_deps(self):
        task = Task()
        st1 = Subtask(parent_id=task.id, status=SubtaskStatus.COMPLETED)
        st2 = Subtask(parent_id=task.id, status=SubtaskStatus.PENDING, depends_on=[st1.id])
        task.subtasks = [st1, st2]

        ready = task.ready_subtasks
        assert len(ready) == 1
        assert ready[0].id == st2.id

    def test_task_update_timestamp(self):
        task = Task()
        old_updated = task.updated_at
        task.update_timestamp()
        assert task.updated_at >= old_updated


class TestSubtask:
    """Tests for Subtask dataclass."""

    def test_subtask_defaults(self):
        st = Subtask()
        assert st.id  # auto-generated UUID
        assert st.status == SubtaskStatus.PENDING
        assert st.depends_on == []
        assert st.assigned_worker is None
        assert st.result is None

    def test_subtask_is_ready(self):
        st = Subtask(status=SubtaskStatus.PENDING)
        assert st.is_ready

    def test_subtask_not_ready_when_assigned(self):
        st = Subtask(status=SubtaskStatus.ASSIGNED)
        assert not st.is_ready

    def test_subtask_is_complete(self):
        st = Subtask(status=SubtaskStatus.COMPLETED)
        assert st.is_complete

    def test_subtask_is_failed(self):
        st = Subtask(status=SubtaskStatus.FAILED)
        assert st.is_failed


class TestWorkerInfo:
    """Tests for WorkerInfo dataclass."""

    def test_worker_available(self):
        wi = WorkerInfo(id="w1", current_load=0, max_capacity=3)
        assert wi.is_available

    def test_worker_not_available(self):
        wi = WorkerInfo(id="w1", current_load=3, max_capacity=3)
        assert not wi.is_available

    def test_worker_partial_load(self):
        wi = WorkerInfo(id="w1", current_load=2, max_capacity=3)
        assert wi.is_available


# ── LLM Client tests ─────────────────────────────────────────────


class TestLLMClient:
    """Tests for LLMClient."""

    def test_init_defaults(self):
        client = LLMClient()
        assert client.provider == "anthropic"
        assert client.model == "claude-sonnet-4-6"
        assert client.max_retries == 5

    def test_init_custom(self):
        client = LLMClient(
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            api_key="test-key",
            max_retries=3,
        )
        assert client.model == "claude-haiku-4-5-20251001"
        assert client.api_key == "test-key"
        assert client.max_retries == 3

    def test_init_openai_provider(self):
        client = LLMClient(provider="openai", api_key="sk-test", model="gpt-4o")
        assert client.provider == "openai"
        assert client.model == "gpt-4o"
        assert client.api_key == "sk-test"

    def test_init_openai_default_model(self):
        client = LLMClient(provider="openai", api_key="sk-test")
        assert client.model == "gpt-4o"

    def test_init_gemini_provider(self):
        client = LLMClient(provider="gemini", api_key="gemini-key")
        assert client.model == "gemini-2.5-pro"

    def test_init_deepseek_provider(self):
        client = LLMClient(provider="deepseek", api_key="ds-key")
        assert client.model == "deepseek/deepseek-chat"

    def test_api_key_env_fallback(self):
        """ANTHROPIC_API_KEY is the universal fallback."""
        import os
        old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-fallback"
        try:
            client = LLMClient(provider="openai")
            assert client.api_key == "anthropic-fallback"
        finally:
            if old is None:
                del os.environ["ANTHROPIC_API_KEY"]
            else:
                os.environ["ANTHROPIC_API_KEY"] = old

    def test_format_tool_anthropic(self):
        tool = make_tool_definition(name="search", description="Search code")
        formatted = LLMClient._format_tool_anthropic(tool)
        assert formatted["name"] == "search"
        assert "input_schema" in formatted
        assert "name" not in formatted.get("function", {})

    def test_format_tool_openai(self):
        tool = make_tool_definition(name="search", description="Search code")
        formatted = LLMClient._format_tool_openai(tool)
        assert formatted["type"] == "function"
        assert formatted["function"]["name"] == "search"
        assert "parameters" in formatted["function"]

    def test_parse_litellm_response_no_tools(self):
        """Parse a litellm ModelResponse with text only."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Hello world"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"
        mock_response.model = "gpt-4o"
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

        client = LLMClient(provider="openai")
        result = client._parse_litellm_response(mock_response)
        assert result.text == "Hello world"
        assert not result.has_tool_calls
        assert result.model == "gpt-4o"
        assert result.usage["input_tokens"] == 10
        assert result.usage["output_tokens"] == 5

    def test_parse_litellm_response_with_tools(self):
        """Parse a litellm ModelResponse with tool calls."""
        mock_tc = MagicMock()
        mock_tc.id = "call_abc"
        mock_tc.function.name = "search"
        mock_tc.function.arguments = '{"query": "test"}'

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = None
        mock_response.choices[0].message.tool_calls = [mock_tc]
        mock_response.choices[0].finish_reason = "tool_calls"
        mock_response.model = "gpt-4o"
        mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=15)

        client = LLMClient(provider="openai")
        result = client._parse_litellm_response(mock_response)
        assert result.has_tool_calls
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].input == {"query": "test"}
        assert result.stop_reason == "tool_use"


class TestLLMResponse:
    """Tests for LLMResponse."""

    def test_no_tool_calls(self):
        resp = LLMResponse(text="Hello")
        assert not resp.has_tool_calls

    def test_with_tool_calls(self):
        tc = ToolCall(id="tc1", name="search", input={"query": "test"})
        resp = LLMResponse(text="", tool_calls=[tc])
        assert resp.has_tool_calls
        assert len(resp.tool_calls) == 1


class TestToolDefinition:
    """Tests for ToolDefinition and make_tool_definition."""

    def test_make_tool_definition(self):
        tool = make_tool_definition(
            name="search",
            description="Search code",
            parameters={
                "query": {
                    "type": "string",
                    "description": "Search query",
                    "required": True,
                },
            },
        )
        assert tool.name == "search"
        assert tool.description == "Search code"
        assert "query" in tool.input_schema["properties"]
        assert "query" in tool.input_schema["required"]

    def test_make_tool_definition_no_params(self):
        tool = make_tool_definition(
            name="list_files",
            description="List files",
        )
        assert tool.name == "list_files"
        assert tool.input_schema["properties"] == {}


# ── Memory types tests ───────────────────────────────────────────


class TestMemoryKey:
    """Tests for MemoryKey."""

    def test_task_scope_valid(self):
        mk = MemoryKey(scope="task", key="decisions", task_id="t1")
        mk.validate()  # Should not raise

    def test_task_scope_missing_task_id(self):
        mk = MemoryKey(scope="task", key="decisions")
        with pytest.raises(ValueError, match="task_id"):
            mk.validate()

    def test_project_scope_valid(self):
        mk = MemoryKey(scope="project", key="architecture", project_id="p1")
        mk.validate()  # Should not raise

    def test_project_scope_missing_project_id(self):
        mk = MemoryKey(scope="project", key="architecture")
        with pytest.raises(ValueError, match="project_id"):
            mk.validate()

    def test_global_scope_valid(self):
        mk = MemoryKey(scope="global", key="conventions")
        mk.validate()  # Should not raise

    def test_invalid_scope(self):
        mk = MemoryKey(scope="invalid", key="test")
        with pytest.raises(ValueError, match="Invalid scope"):
            mk.validate()


class TestMemoryEntry:
    """Tests for MemoryEntry."""

    def test_from_dict(self):
        entry = MemoryEntry.from_dict(
            {
                "id": "mem-1",
                "key": {"scope": "task", "key": "decisions", "task_id": "t1"},
                "content": "Use PostgreSQL",
                "content_type": "text",
                "source_agent": "orchestrator",
                "importance": 0.7,
                "tags": ["architecture"],
            }
        )
        assert entry.id == "mem-1"
        assert entry.key.scope == "task"
        assert entry.key.key == "decisions"
        assert entry.content == "Use PostgreSQL"
        assert entry.importance == 0.7


# ── ShortTermMemory tests ────────────────────────────────────────


class TestShortTermMemory:
    """Tests for ShortTermMemory wrapper."""

    def _make_engine(self):
        engine = MagicMock()
        engine.read_memory.return_value = None
        engine.write_memory.return_value = {"id": "mem-1", "content": "test"}
        return engine

    def test_read_not_found(self):
        engine = self._make_engine()
        stm = ShortTermMemory(engine)
        result = stm.read("decisions", task_id="t1")
        assert result is None

    def test_read_found_dict(self):
        engine = self._make_engine()
        engine.read_memory.return_value = {
            "id": "mem-1",
            "key": {"scope": "task", "key": "decisions", "task_id": "t1"},
            "content": "Use PostgreSQL",
        }
        stm = ShortTermMemory(engine)
        result = stm.read("decisions", task_id="t1")
        assert result is not None
        assert result.content == "Use PostgreSQL"

    def test_write(self):
        engine = self._make_engine()
        stm = ShortTermMemory(engine)
        stm.write("decisions", "Use PostgreSQL", task_id="t1")
        engine.write_memory.assert_called_once()
        call_kwargs = engine.write_memory.call_args
        assert call_kwargs[1]["key_scope"] == "task"
        assert call_kwargs[1]["key"] == "decisions"

    def test_delete(self):
        engine = self._make_engine()
        stm = ShortTermMemory(engine)
        stm.delete("decisions", task_id="t1")
        engine.delete_memory.assert_called_once()


# ── LongTermMemory tests ─────────────────────────────────────────


class TestLongTermMemory:
    """Tests for LongTermMemory wrapper."""

    def _make_engine(self):
        engine = MagicMock()
        engine.read_memory.return_value = None
        engine.write_memory.return_value = {"id": "mem-1", "content": "test"}
        engine.search_memory.return_value = []
        return engine

    def test_read_not_found(self):
        engine = self._make_engine()
        ltm = LongTermMemory(engine)
        result = ltm.read("architecture", project_id="p1")
        assert result is None

    def test_write_project_scope(self):
        engine = self._make_engine()
        ltm = LongTermMemory(engine)
        ltm.write("architecture", "Microservices", project_id="p1", importance=0.8)
        call_kwargs = engine.write_memory.call_args
        assert call_kwargs[1]["key_scope"] == "project"
        assert call_kwargs[1]["importance"] == 0.8

    def test_write_global_scope(self):
        engine = self._make_engine()
        ltm = LongTermMemory(engine)
        ltm.write("conventions", "Use snake_case")
        call_kwargs = engine.write_memory.call_args
        assert call_kwargs[1]["key_scope"] == "global"

    def test_search(self):
        engine = self._make_engine()
        engine.search_memory.return_value = []
        ltm = LongTermMemory(engine)
        results = ltm.search("event sourcing", project_id="p1")
        assert results == []
        engine.search_memory.assert_called_once_with(
            query="event sourcing",
            scope_type="project",
            project_id="p1",
            max_results=20,
            min_score=0.5,
        )


# ── Orchestrator tests ───────────────────────────────────────────


class TestOrchestrator:
    """Tests for Orchestrator."""

    def test_init_defaults(self):
        orch = Orchestrator()
        assert orch.engine is None
        assert orch.llm_client is None
        assert orch.config.max_subtasks == 10
        assert orch.workers == {}
        assert orch.tasks == {}

    @pytest.mark.asyncio
    async def test_register_worker(self):
        orch = Orchestrator()
        wi = WorkerInfo(id="w1", capabilities=["code"])
        await orch.register_worker(wi)
        assert "w1" in orch.workers

    @pytest.mark.asyncio
    async def test_unregister_worker(self):
        orch = Orchestrator()
        wi = WorkerInfo(id="w1", capabilities=["code"])
        await orch.register_worker(wi)
        await orch.unregister_worker("w1")
        assert "w1" not in orch.workers

    @pytest.mark.asyncio
    async def test_get_available_workers(self):
        orch = Orchestrator()
        await orch.register_worker(WorkerInfo(id="w1", current_load=0, max_capacity=3))
        await orch.register_worker(WorkerInfo(id="w2", current_load=3, max_capacity=3))
        available = orch.get_available_workers()
        assert len(available) == 1
        assert available[0].id == "w1"

    @pytest.mark.asyncio
    async def test_select_worker(self):
        orch = Orchestrator()
        await orch.register_worker(WorkerInfo(id="w1", current_load=1, max_capacity=3))
        await orch.register_worker(WorkerInfo(id="w2", current_load=0, max_capacity=3))
        subtask = Subtask()
        selected = orch._select_worker(subtask)
        assert selected == "w2"  # Lower load

    def test_select_worker_none_available(self):
        orch = Orchestrator()
        subtask = Subtask()
        selected = orch._select_worker(subtask)
        assert selected is None

    def test_parse_decomposition(self):
        orch = Orchestrator()
        response = LLMResponse(
            text=json.dumps(
                [
                    {
                        "description": "Research existing auth code",
                        "depends_on": [],
                        "file_constraints": [],
                        "expected_output": "List of auth-related files",
                    },
                    {
                        "description": "Implement login endpoint",
                        "depends_on": [0],
                        "file_constraints": ["src/utils.py"],
                        "expected_output": "Working login endpoint",
                    },
                ]
            ),
        )

        subtasks = orch._parse_decomposition(response, "task-1")
        assert len(subtasks) == 2
        assert subtasks[0].description == "Research existing auth code"
        assert subtasks[0].depends_on == []
        assert subtasks[1].description == "Implement login endpoint"
        assert subtasks[1].depends_on == [subtasks[0].id]
        assert subtasks[1].file_constraints == ["src/utils.py"]

    def test_parse_decomposition_with_markdown(self):
        orch = Orchestrator()
        response = LLMResponse(
            text=(
                '```json\n[{"description": "Task 1", '
                '"depends_on": [], "file_constraints": [], '
                '"expected_output": "Done"}]\n```'
            ),
        )

        subtasks = orch._parse_decomposition(response, "task-1")
        assert len(subtasks) == 1
        assert subtasks[0].description == "Task 1"

    def test_parse_decomposition_invalid_json(self):
        orch = Orchestrator()
        response = LLMResponse(text="not json at all")

        with pytest.raises(RuntimeError, match="Failed to parse"):
            orch._parse_decomposition(response, "task-1")

    @pytest.mark.asyncio
    async def test_get_task_status(self):
        orch = Orchestrator()
        task = Task(id="t1", description="test")
        orch.tasks["t1"] = task

        result = await orch.get_task_status("t1")
        assert result is task

    @pytest.mark.asyncio
    async def test_get_task_status_not_found(self):
        orch = Orchestrator()
        result = await orch.get_task_status("nonexistent")
        assert result is None

    def test_aggregate_results(self):
        orch = Orchestrator()
        task = Task(id="t1")
        st1 = Subtask(
            id="s1",
            parent_id="t1",
            status=SubtaskStatus.COMPLETED,
            result=SubtaskResult(subtask_id="s1", summary="Did research"),
        )
        st2 = Subtask(
            id="s2",
            parent_id="t1",
            status=SubtaskStatus.FAILED,
            result=SubtaskResult(subtask_id="s2", summary="Bug in code", success=False),
        )
        task.subtasks = [st1, st2]

        result = orch._aggregate_results(task)
        assert "Completed 1/2 subtasks" in result
        assert "Failed 1 subtasks" in result
        assert "Did research" in result
        assert "Bug in code" in result


# ── Worker tests ─────────────────────────────────────────────────


class TestWorker:
    """Tests for Worker."""

    def test_init_defaults(self):
        worker = Worker()
        assert worker.worker_id  # auto-generated
        assert worker.capabilities == ["code", "search", "memory", "test"]
        assert worker.current_task is None

    def test_init_custom(self):
        worker = Worker(
            worker_id="w1",
            capabilities=["rust", "search"],
            max_capacity=5,
        )
        assert worker.worker_id == "w1"
        assert worker.capabilities == ["rust", "search"]
        assert worker.max_capacity == 5

    def test_get_info(self):
        worker = Worker(worker_id="w1", capabilities=["code"], max_capacity=3)
        info = worker.get_info()
        assert info.id == "w1"
        assert info.capabilities == ["code"]
        assert info.max_capacity == 3
        assert info.current_load == 0

    def test_tools_built(self):
        worker = Worker()
        assert "search" in worker.tools
        assert "read_memory" in worker.tools
        assert "write_memory" in worker.tools
        assert "edit_file" in worker.tools
        assert "search_memory" in worker.tools
        assert "read_file" in worker.tools
        assert "list_files" in worker.tools

    def test_tool_definitions_built(self):
        worker = Worker()
        assert len(worker._tool_definitions) == 12
        names = [t.name for t in worker._tool_definitions]
        assert "search" in names
        assert "read_memory" in names
        assert "write_memory" in names
        assert "edit_file" in names
        assert "search_memory" in names
        assert "read_file" in names
        assert "list_files" in names

    @pytest.mark.asyncio
    async def test_execute_subtask_no_llm(self):
        worker = Worker(worker_id="w1", execution_mode="llm")
        subtask = Subtask(id="s1", parent_id="t1", description="Do something")

        result = await worker.execute_subtask(subtask)
        assert not result.success
        assert "No LLM client" in result.summary

    def test_collect_modified_files_ignores_reads(self):
        """read_file tool calls should NOT appear in modified files."""
        worker = Worker.__new__(Worker)
        tool_log = [
            {
                "tool_call": {"name": "read_file", "input": {"file_path": "/tmp/read.py"}},
                "result": "file content here",
            },
            {
                "tool_call": {
                    "name": "edit_file",
                    "input": {"file_path": "/tmp/edit.py", "content": "new", "create": False},
                },
                "result": "ok",
            },
        ]
        changes = worker._collect_modified_files(tool_log)
        # read_file should NOT be in the results
        assert all(c.file_path != "/tmp/read.py" for c in changes)
        # edit_file SHOULD be in the results
        assert any(c.file_path == "/tmp/edit.py" for c in changes)

    @pytest.mark.asyncio
    async def test_heartbeat(self):
        worker = Worker(worker_id="w1")
        hb = await worker.send_heartbeat()
        assert hb["worker_id"] == "w1"
        assert "timestamp" in hb


# ── Integration-style tests (no external services) ───────────────


class TestOrchestratorWorkerIntegration:
    """Integration tests using mock LLM and no engine."""

    @pytest.mark.asyncio
    async def test_decompose_and_assign(self):
        """Test the full flow: decompose task -> assign subtasks."""
        orch = Orchestrator()

        # Register a worker
        await orch.register_worker(WorkerInfo(id="w1", capabilities=["code"]))

        # Parse decomposition manually (skip LLM)
        response = LLMResponse(
            text=json.dumps(
                [
                    {
                        "description": "Research auth code",
                        "depends_on": [],
                        "file_constraints": [],
                        "expected_output": "Find auth files",
                    },
                    {
                        "description": "Implement auth",
                        "depends_on": [0],
                        "file_constraints": ["README.md"],
                        "expected_output": "Auth module",
                    },
                ]
            ),
        )
        subtasks = orch._parse_decomposition(response, "task-1")
        assert len(subtasks) == 2

        # Assign the first subtask
        assigned = await orch.assign_subtask(subtasks[0])
        assert assigned == "w1"
        assert subtasks[0].status == SubtaskStatus.ASSIGNED
        assert subtasks[0].assigned_worker == "w1"

        # Second subtask should not be ready yet (depends on first)
        task = Task(id="task-1", subtasks=subtasks, status=TaskStatus.IN_PROGRESS)
        ready = task.ready_subtasks
        assert len(ready) == 0  # Second subtask depends on first

    @pytest.mark.asyncio
    async def test_handle_subtask_result(self):
        """Test handling a successful subtask result."""
        orch = Orchestrator()

        await orch.register_worker(WorkerInfo(id="w1", current_load=1, max_capacity=3))

        st = Subtask(id="s1", parent_id="t1", assigned_worker="w1")
        task = Task(id="t1", subtasks=[st], status=TaskStatus.IN_PROGRESS)
        orch.tasks["t1"] = task

        result = SubtaskResult(
            subtask_id="s1",
            worker_id="w1",
            summary="Completed successfully",
            success=True,
        )

        await orch.handle_subtask_result(result)

        assert st.status == SubtaskStatus.COMPLETED
        assert task.is_complete
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_handle_subtask_failure(self):
        """Test handling a failed subtask result."""
        orch = Orchestrator()
        await orch.register_worker(WorkerInfo(id="w1", current_load=1, max_capacity=3))

        st = Subtask(id="s1", parent_id="t1", assigned_worker="w1")
        task = Task(id="t1", subtasks=[st], status=TaskStatus.IN_PROGRESS)
        orch.tasks["t1"] = task

        result = SubtaskResult(
            subtask_id="s1",
            worker_id="w1",
            summary="Something went wrong",
            success=False,
        )

        await orch.handle_subtask_result(result)

        assert st.status == SubtaskStatus.FAILED
        assert task.has_failed
        assert task.status == TaskStatus.FAILED
