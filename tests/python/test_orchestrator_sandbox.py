"""Unit tests for Orchestrator sandbox decomposition path."""

from __future__ import annotations

import json

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.sandbox import (
    ExecResult,
    SandboxConfig,
    SandboxManager,
)

# ── Orchestrator sandbox initialization tests ────────────────────

class TestOrchestratorSandboxInit:
    """Tests for Orchestrator initialization with sandbox_manager."""

    def test_sandbox_manager_set(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(
            engine=None,
            llm_client=None,
            sandbox_manager=sm,
        )
        assert orch.sandbox_manager is sm
        assert orch.llm_client is None

    def test_llm_client_takes_precedence(self):
        """When both are provided, llm_client is used (sandbox is fallback)."""
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)

        class FakeLLMClient:
            pass

        fake = FakeLLMClient()
        orch = Orchestrator(
            engine=None,
            llm_client=fake,
            sandbox_manager=sm,
        )
        assert orch.llm_client is fake
        assert orch.sandbox_manager is sm


# ── _parse_decomposition_items tests ────────────────────────────

class TestParseDecompositionItems:
    """Tests for Orchestrator._parse_decomposition_items()."""

    def test_single_subtask(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(engine=None, llm_client=None, sandbox_manager=sm)

        items = [
            {
                "description": "Fix bug",
                "depends_on": [],
                "file_constraints": [],
                "expected_output": "Bug fixed",
            },
        ]
        subtasks = orch._parse_decomposition_items(items, "task-1")

        assert len(subtasks) == 1
        assert subtasks[0].description == "Fix bug"
        assert subtasks[0].parent_id == "task-1"
        assert subtasks[0].depends_on == []

    def test_multiple_subtasks_with_deps(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(engine=None, llm_client=None, sandbox_manager=sm)

        items = [
            {"description": "Research", "depends_on": []},
            {"description": "Implement", "depends_on": [0]},
            {"description": "Test", "depends_on": [1]},
        ]
        subtasks = orch._parse_decomposition_items(items, "task-2")

        assert len(subtasks) == 3
        assert subtasks[0].depends_on == []
        assert subtasks[1].depends_on == [subtasks[0].id]
        assert subtasks[2].depends_on == [subtasks[1].id]

    def test_empty_items(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(engine=None, llm_client=None, sandbox_manager=sm)

        subtasks = orch._parse_decomposition_items([], "task-3")
        assert subtasks == []

    def test_non_dict_items_skipped(self):
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(engine=None, llm_client=None, sandbox_manager=sm)

        items = ["bad", {"description": "Good task", "depends_on": []}, None]
        subtasks = orch._parse_decomposition_items(items, "task-4")
        assert len(subtasks) == 1
        assert subtasks[0].description == "Good task"


# ── decompose_task sandbox path integration test ─────────────────

class TestDecomposeSandboxPath:
    """Integration tests for decompose_task() with sandbox path."""

    @pytest.mark.asyncio
    async def test_sandbox_path_called(self, monkeypatch):
        """Verify sandbox path is used when llm_client is None."""
        config = SandboxConfig(project_path="/tmp/test")
        sm = SandboxManager(config)
        orch = Orchestrator(engine=None, llm_client=None, sandbox_manager=sm)

        # Mock the subprocess execution to return valid JSON
        fake_json = json.dumps([
            {"description": "Step 1", "depends_on": []},
            {"description": "Step 2", "depends_on": [0]},
        ])
        fake_result = ExecResult(exit_code=0, stdout=fake_json)

        async def fake_execute_subprocess(request):
            return fake_result

        monkeypatch.setattr(sm, "_execute_subprocess", fake_execute_subprocess)

        from ultimate_coders.agent.types import Task
        task = Task(description="Test task", project_id="test-project")

        subtasks = await orch.decompose_task(task)
        assert len(subtasks) == 2
        assert subtasks[0].description == "Step 1"
        assert subtasks[1].depends_on == [subtasks[0].id]

    @pytest.mark.asyncio
    async def test_llm_path_still_works(self):
        """Ensure traditional LLM path is not broken."""
        # Without sandbox_manager, should raise about needing llm_client
        orch = Orchestrator(engine=None, llm_client=None, sandbox_manager=None)

        from ultimate_coders.agent.types import Task
        task = Task(description="Test task")

        with pytest.raises(RuntimeError, match="Either llm_client or sandbox_manager"):
            await orch.decompose_task(task)
