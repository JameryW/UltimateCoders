"""LLM decomposition wiring in Orchestrator.submit_task.

Tests cover:
- LLM decomposition path (llm_client.complete returns valid JSON → subtasks created)
- Fallback to newline-split when llm_client is None
- Fallback to newline-split when complete() raises
- Fallback to newline-split when parse fails (bad JSON)
- Subtask shape mapping (depends_on, file_constraints, expected_output)
- Fallback when LLM returns empty text
- agent_config + project_id propagation through LLM path
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.types import SubtaskStatus, TaskStatus


def _llm_response(text: str) -> MagicMock:
    """Build a mock LLMResponse with .text."""
    resp = MagicMock()
    resp.text = text
    return resp


def _subtask_json_list(items: list[dict]) -> str:
    """Serialize subtask dicts to JSON (what the LLM outputs)."""
    return json.dumps(items)


def _st(
    desc: str,
    depends_on: list | None = None,
    files: list[str] | None = None,
    expected: str = "",
) -> dict:
    """Build a subtask dict compactly (matches LLM output schema)."""
    return {
        "description": desc,
        "depends_on": depends_on or [],
        "file_constraints": files or [],
        "expected_output": expected,
    }


# ── LLM decomposition path ────────────────────────────────────


class TestLLMDecomposition:
    """Verify submit_task uses LLM decomposition when llm_client is available."""

    async def test_llm_decompose_creates_subtasks(self):
        """LLM returns valid JSON → subtasks created from LLM output."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st("Add foo() to bar.py", files=["bar.py"], expected="foo() defined"),
            _st("Call foo() from main", [1], ["main.py"], "main calls foo()"),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Implement foo and call it", task_id="t-1")

        assert task.id == "t-1"
        assert len(task.subtasks) == 2
        assert task.status == TaskStatus.IN_PROGRESS

        st0 = task.subtasks[0]
        assert st0.id == "t-1-s0"
        assert st0.description == "Add foo() to bar.py"
        assert st0.depends_on == []
        assert st0.file_constraints == ["bar.py"]
        assert st0.expected_output == "foo() defined"
        assert st0.status == SubtaskStatus.PENDING

        st1 = task.subtasks[1]
        assert st1.id == "t-1-s1"
        assert st1.description == "Call foo() from main"
        # 1-based index 1 → 0-based subtask s0
        assert st1.depends_on == ["t-1-s0"]
        assert st1.file_constraints == ["main.py"]
        assert st1.expected_output == "main calls foo()"

        llm.complete.assert_called_once()

    async def test_depends_on_multiple_indices(self):
        """Multiple 1-based depends_on indices are mapped correctly."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st("Step A"),
            _st("Step B"),
            _st("Step C (depends on A and B)", [1, 2]),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Three steps", task_id="t-2")

        assert len(task.subtasks) == 3
        assert task.subtasks[2].depends_on == ["t-2-s0", "t-2-s1"]

    async def test_agent_config_propagated_to_llm_subtasks(self):
        """agent_config passed to submit_task is applied to each LLM subtask."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st("Do thing"),
        ])))
        cfg = {"tools": ["Edit"], "agent_name": "grok-build"}

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Do thing", task_id="t-3", agent_config=cfg)

        assert task.subtasks[0].agent_config == cfg

    async def test_project_id_propagated_to_llm_subtasks(self):
        """project_id is set on each LLM-decomposed subtask."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st("Do thing"),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Do thing", task_id="t-4", project_id="proj-x")

        assert task.subtasks[0].project_id == "proj-x"
        assert task.project_id == "proj-x"

    async def test_llm_called_with_messages_and_system(self):
        """complete() is called with messages list + system prompt."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response("[]"))

        orch = Orchestrator(llm_client=llm)
        await orch.submit_task("Some task", task_id="t-5")

        call_kwargs = llm.complete.call_args
        assert "messages" in call_kwargs.kwargs
        assert isinstance(call_kwargs.kwargs["messages"], list)
        assert call_kwargs.kwargs["messages"][0]["role"] == "user"
        assert "Some task" in call_kwargs.kwargs["messages"][0]["content"]
        assert call_kwargs.kwargs["system"] is not None
        # Default raised for thinking-style local models (Qwen3 distills);
        # see UC_LLM_DECOMPOSE_MAX_TOKENS in orchestrator._decompose_task.
        assert call_kwargs.kwargs["max_tokens"] == 4096

    async def test_llm_subtasks_with_markdown_fences(self):
        """parse_decomposition_output strips markdown fences — end-to-end."""
        raw = '```json\n' + _subtask_json_list([_st("Fenced task")]) + '\n```'
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(raw))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Fenced", task_id="t-6")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Fenced task"


# ── Fallback to newline-split ─────────────────────────────────


class TestNewlineSplitFallback:
    """Verify submit_task falls back to newline-split when LLM fails."""

    async def test_llm_client_none_falls_back(self):
        """No llm_client → newline-split, no exception."""
        orch = Orchestrator(llm_client=None)
        task = await orch.submit_task("Line one\nLine two", task_id="t-7")

        assert len(task.subtasks) == 2
        assert task.subtasks[0].description == "Line one"
        assert task.subtasks[1].description == "Line two"
        assert task.subtasks[0].depends_on == []
        assert task.subtasks[1].depends_on == []

    async def test_llm_complete_raises_falls_back(self):
        """complete() raises → graceful fallback to newline-split."""
        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("API down"))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Task A\nTask B", task_id="t-8")

        assert len(task.subtasks) == 2
        assert task.subtasks[0].description == "Task A"

    async def test_llm_returns_bad_json_falls_back(self):
        """LLM returns unparseable text → fallback to newline-split."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response("this is not json at all"),
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Single line task", task_id="t-9")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Single line task"

    async def test_llm_returns_empty_text_falls_back(self):
        """LLM returns empty string → fallback to newline-split."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(""))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Do something", task_id="t-10")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Do something"

    async def test_llm_returns_empty_array_falls_back(self):
        """LLM returns [] (empty array) → fallback to newline-split."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response("[]"))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Only task", task_id="t-11")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Only task"

    async def test_fallback_preserves_agent_config(self):
        """agent_config is applied to newline-split subtasks on fallback."""
        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("down"))
        cfg = {"agent_name": "claude-code"}

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("A\nB", task_id="t-12", agent_config=cfg)

        assert task.subtasks[0].agent_config == cfg
        assert task.subtasks[1].agent_config == cfg

    async def test_fallback_preserves_project_id(self):
        """project_id is set on newline-split subtasks on fallback."""
        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("down"))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("A", task_id="t-13", project_id="proj-y")

        assert task.subtasks[0].project_id == "proj-y"


# ── Edge cases ────────────────────────────────────────────────


class TestDecompositionEdgeCases:
    """Edge cases in subtask mapping."""

    async def test_invalid_depends_on_ignored(self):
        """Non-integer depends_on entries are silently ignored."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st("A"),
            _st("B", ["invalid", 1]),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Two steps", task_id="t-14")

        assert len(task.subtasks) == 2
        # "invalid" is ignored, 1 → s0
        assert task.subtasks[1].depends_on == ["t-14-s0"]

    async def test_out_of_range_depends_on_ignored(self):
        """depends_on index out of range (e.g. 5 when only 2 subtasks)."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st("A"),
            _st("B", [5]),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Two steps", task_id="t-15")

        assert len(task.subtasks) == 2
        assert task.subtasks[1].depends_on == []

    async def test_missing_optional_fields_default(self):
        """Subtasks missing file_constraints/expected_output get defaults."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(
            _subtask_json_list([{"description": "No constraints"}]),
        ))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Minimal", task_id="t-16")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].file_constraints == []
        assert task.subtasks[0].expected_output == ""
        assert task.subtasks[0].depends_on == []

    async def test_subtask_missing_description_skipped(self):
        """A subtask dict with empty description is skipped.

        The valid subtask keeps its original enumerate-based index (s1),
        not a renumbered s0 — this preserves depends_on index consistency
        (LLM's 1-based indices reference original array positions).
        """
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st(""),
            _st("Valid"),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Skip empty", task_id="t-17")

        # Only the valid subtask is kept, retains original index s1
        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Valid"
        assert task.subtasks[0].id == "t-17-s1"

    async def test_all_subtasks_empty_description_falls_back(self):
        """All LLM subtasks have empty descriptions → newline-split fallback."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st(""),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Fallback me", task_id="t-18")

        # _decompose_task returns None (no valid subtasks) → newline-split
        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Fallback me"

    async def test_non_dict_items_skipped(self):
        """Non-dict items in the JSON array are skipped."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(
            json.dumps(["not a dict", _st("Valid")]),
        ))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Mixed", task_id="t-19")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Valid"

    async def test_llm_result_is_string(self):
        """If complete() returns a raw string (duck-typed), used as text."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_subtask_json_list([
            _st("String result"),
        ]))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("String result", task_id="t-20")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "String result"

    async def test_task_stored_in_tasks_dict(self):
        """Submitted task is stored in orchestrator.tasks for both paths."""
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=_llm_response(_subtask_json_list([
            _st("Stored"),
        ])))

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Stored task", task_id="t-21")

        assert "t-21" in orch.tasks
        assert orch.tasks["t-21"] is task
