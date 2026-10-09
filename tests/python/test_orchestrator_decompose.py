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

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator, validate_decomposition
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
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st("Add foo() to bar.py", files=["bar.py"], expected="foo() defined"),
                        _st("Call foo() from main", [1], ["main.py"], "main calls foo()"),
                    ]
                )
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Implement foo and call it", task_id="t-1")

        assert task.id == "t-1"
        assert len(task.subtasks) == 2
        assert task.status == TaskStatus.IN_PROGRESS

        st0 = task.subtasks[0]
        assert st0.id == "t-1-s0"
        assert st0.description == "Add foo() to bar.py"
        assert st0.user_request == "Implement foo and call it"
        assert st0.depends_on == []
        assert st0.file_constraints == ["bar.py"]
        assert st0.expected_output == "foo() defined"
        assert st0.status == SubtaskStatus.PENDING

        st1 = task.subtasks[1]
        assert st1.id == "t-1-s1"
        assert st1.description == "Call foo() from main"
        assert st1.user_request == "Implement foo and call it"
        # 1-based index 1 → 0-based subtask s0
        assert st1.depends_on == ["t-1-s0"]
        assert st1.file_constraints == ["main.py"]
        assert st1.expected_output == "main calls foo()"

        llm.complete.assert_called_once()

    async def test_depends_on_multiple_indices(self):
        """Multiple 1-based depends_on indices are mapped correctly."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st("Step A"),
                        _st("Step B"),
                        _st("Step C (depends on A and B)", [1, 2]),
                    ]
                )
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Three steps", task_id="t-2")

        assert len(task.subtasks) == 3
        assert task.subtasks[2].depends_on == ["t-2-s0", "t-2-s1"]

    async def test_agent_config_propagated_to_llm_subtasks(self):
        """agent_config passed to submit_task is applied to each LLM subtask."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st("Do thing"),
                    ]
                )
            )
        )
        cfg = {"tools": ["Edit"], "agent_name": "grok-build"}

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Do thing", task_id="t-3", agent_config=cfg)

        assert task.subtasks[0].agent_config == cfg

    async def test_project_id_propagated_to_llm_subtasks(self):
        """project_id is set on each LLM-decomposed subtask."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st("Do thing"),
                    ]
                )
            )
        )

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
        raw = "```json\n" + _subtask_json_list([_st("Fenced task")]) + "\n```"
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
        assert llm.complete.await_count == 2

    @pytest.mark.parametrize(
        "invalid",
        ["not json", '{"description": "not an array"}', '[{"description": null}]'],
    )
    async def test_invalid_output_is_repaired_once(self, invalid):
        llm = MagicMock()
        llm.complete = AsyncMock(
            side_effect=[
                _llm_response(invalid),
                _llm_response(_subtask_json_list([_st("Repaired plan")])),
            ]
        )

        task = await Orchestrator(llm_client=llm).submit_task("Original request")

        assert llm.complete.await_count == 2
        assert [st.description for st in task.subtasks] == ["Repaired plan"]
        assert "rejected:" in llm.complete.await_args.kwargs["messages"][0]["content"]

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
        """A non-numeric depends_on entry rejects the plan, not just that entry.

        Silently dropping the entry changed what the plan meant: the subtask was
        meant to wait for something and now runs immediately. That is
        "unreasonable", so it costs a re-decompose -- and when the model repeats
        the defect, the caller falls back to newline-split rather than shipping
        a plan nobody asked for.
        """
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st("A"),
                        _st("B", ["invalid", 1]),
                    ]
                )
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Two steps", task_id="t-14")

        # Asked twice: once, then once more with the defect named.
        assert llm.complete.await_count == 2
        corrective = llm.complete.await_args_list[1].kwargs["messages"][0]["content"]
        assert "non-numeric depends_on entry" in corrective
        # Still unreasonable -> the newline-split fallback, not the LLM plan.
        assert [s.description for s in task.subtasks] == ["Two steps"]

    async def test_out_of_range_depends_on_ignored(self):
        """A depends_on index outside 1..N rejects the plan.

        Dropping an out-of-range dependency is the worst silent failure here:
        the subtask is released to run before the step it named. Rejected, so
        the defect is either repaired by the re-decompose or the plan is not
        used at all.
        """
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st("A"),
                        _st("B", [5]),
                    ]
                )
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Two steps", task_id="t-15")

        assert llm.complete.await_count == 2
        corrective = llm.complete.await_args_list[1].kwargs["messages"][0]["content"]
        assert "outside 1..2" in corrective
        assert [s.description for s in task.subtasks] == ["Two steps"]

    async def test_redecompose_happens_exactly_once(self):
        """We ask twice, not until it passes -- and the retry carries the reason.

        A model that cannot produce a sound plan in two tries must not spin: the
        caller's newline-split fallback is the third and final answer.
        """
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(_subtask_json_list([_st("A"), _st("A")]))
        )

        orch = Orchestrator(llm_client=llm)
        await orch.submit_task("Degenerate", task_id="t-degen")

        assert llm.complete.await_count == 2
        first = llm.complete.await_args_list[0].kwargs["messages"][0]["content"]
        second = llm.complete.await_args_list[1].kwargs["messages"][0]["content"]
        assert "rejected:" not in first
        assert "same work" in second

    async def test_redecompose_repairs_and_stops(self):
        """A repairable defect costs one extra call and then succeeds."""
        good = _llm_response(_subtask_json_list([_st("A"), _st("B", [1])]))
        bad = _llm_response(_subtask_json_list([_st("A"), _st("B", [9])]))
        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=[bad, good])

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Two steps", task_id="t-repair")

        assert llm.complete.await_count == 2
        assert [s.description for s in task.subtasks] == ["A", "B"]
        assert task.subtasks[1].depends_on == ["t-repair-s0"]

    async def test_missing_optional_fields_default(self):
        """Subtasks missing file_constraints/expected_output get defaults."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list([{"description": "No constraints"}]),
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Minimal", task_id="t-16")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].file_constraints == []
        assert task.subtasks[0].expected_output == ""
        assert task.subtasks[0].depends_on == []

    async def test_subtask_missing_description_rejects_plan(self):
        """An item with no description rejects the plan; nothing is dropped.

        The previous contract dropped the item and kept the survivor's original
        enumerate-based index (s1 rather than a renumbered s0) so that
        depends_on's 1-based indices still lined up. That index invariant is
        still the builder's behaviour, but it is no longer load-bearing for this
        case: a plan is either sound and used whole, or rejected. Dropping items
        is what made the indices matter, and dropping items is what the quality
        gate is there to prevent.
        """
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st(""),
                        _st("Valid"),
                    ]
                )
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Skip empty", task_id="t-17")

        assert llm.complete.await_count == 2
        corrective = llm.complete.await_args_list[1].kwargs["messages"][0]["content"]
        assert "has no description" in corrective
        assert [s.description for s in task.subtasks] == ["Skip empty"]

    async def test_all_subtasks_empty_description_falls_back(self):
        """All LLM subtasks have empty descriptions → newline-split fallback."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st(""),
                    ]
                )
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Fallback me", task_id="t-18")

        # _decompose_task returns None (no valid subtasks) → newline-split
        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "Fallback me"

    async def test_non_dict_items_reject_plan(self):
        """A non-object item rejects the plan instead of being skipped."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                json.dumps(["not a dict", _st("Valid")]),
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Mixed", task_id="t-19")

        assert llm.complete.await_count == 2
        corrective = llm.complete.await_args_list[1].kwargs["messages"][0]["content"]
        assert "is not an object" in corrective
        assert [s.description for s in task.subtasks] == ["Mixed"]

    async def test_llm_result_is_string(self):
        """If complete() returns a raw string (duck-typed), used as text."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_subtask_json_list(
                [
                    _st("String result"),
                ]
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("String result", task_id="t-20")

        assert len(task.subtasks) == 1
        assert task.subtasks[0].description == "String result"

    async def test_task_stored_in_tasks_dict(self):
        """Submitted task is stored in orchestrator.tasks for both paths."""
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=_llm_response(
                _subtask_json_list(
                    [
                        _st("Stored"),
                    ]
                )
            )
        )

        orch = Orchestrator(llm_client=llm)
        task = await orch.submit_task("Stored task", task_id="t-21")

        assert "t-21" in orch.tasks
        assert orch.tasks["t-21"] is task


class TestValidateDecomposition:
    """The quality gate itself, apart from the LLM.

    These are the defects the builder used to absorb silently or not see at
    all. Each one has to name a reason -- the re-decompose prompt echoes it, and
    an unnamed reason is a useless prompt.
    """

    def test_sound_plan_passes(self):
        assert validate_decomposition([_st("A"), _st("B", [1])]) is None

    def test_empty_and_wrong_shapes_name_the_defect(self):
        assert validate_decomposition([]) == "decomposition produced no subtask items"
        assert validate_decomposition(None) == "decomposition produced no subtask items"
        assert validate_decomposition("nope") == "decomposition produced no subtask items"

    def test_non_object_item(self):
        assert validate_decomposition(["not a dict", _st("B")]) == "item 0 is not an object"

    def test_missing_description(self):
        assert validate_decomposition([_st(""), _st("B")]) == "item 0 has no description"

    def test_non_numeric_dependency(self):
        got = validate_decomposition([_st("A"), _st("B", ["first"])])
        assert got == "item 1 has a non-numeric depends_on entry 'first'"

    def test_non_list_dependency(self):
        got = validate_decomposition([_st("A"), _st("B", "1")])
        assert got == "item 1 has a non-list depends_on"

    def test_out_of_range_dependency_names_the_range(self):
        got = validate_decomposition([_st("A"), _st("B", [9])])
        assert got == "item 1 depends on index 9, which is outside 1..2"

    def test_self_dependency(self):
        assert validate_decomposition([_st("A", [1])]) == "item 0 depends on itself"

    def test_two_node_cycle(self):
        # 1 -> 2 -> 1 in 1-based terms; neither can ever become ready.
        got = validate_decomposition([_st("A", [2]), _st("B", [1])])
        assert got is not None and "dependency cycle" in got, got

    def test_long_cycle_is_found_not_just_the_first_edge(self):
        got = validate_decomposition([_st("A", [3]), _st("B", [1]), _st("C", [2])])
        assert got is not None and "dependency cycle" in got, got

    def test_acyclic_chain_of_three_is_fine(self):
        assert validate_decomposition([_st("A"), _st("B", [1]), _st("C", [2])]) is None

    def test_degenerate_all_identical(self):
        got = validate_decomposition([_st("same"), _st("same")])
        assert got == "all 2 items describe the same work"

    def test_single_item_is_not_degenerate(self):
        assert validate_decomposition([_st("only")]) is None

    def test_reason_is_specific_enough_to_prompt_again(self):
        """The reason goes into the next prompt, so it must name the place."""
        got = validate_decomposition([_st("A"), _st("B", [5])])
        assert "item 1" in got and "1..2" in got

    @pytest.mark.parametrize("description", [None, 123, [], {}])
    def test_non_string_description_is_rejected(self, description):
        assert validate_decomposition([{"description": description}]) is not None

    @pytest.mark.parametrize("dep", [True, False, 1.5, 1.0, None, {}, []])
    def test_dependency_cannot_be_lossily_coerced(self, dep):
        problem = validate_decomposition([_st("A"), _st("B", [dep])])
        assert problem is not None and "non-integer" in problem

    def test_legacy_integer_string_dependency_still_passes(self):
        assert validate_decomposition([_st("A"), _st("B", ["1"])]) is None

    @pytest.mark.parametrize("files", ["src/main.rs", [1], {}])
    def test_invalid_file_constraints_are_rejected(self, files):
        item = _st("A")
        item["file_constraints"] = files
        assert "file_constraints" in validate_decomposition([item])

    def test_invalid_expected_output_is_rejected(self):
        item = _st("A")
        item["expected_output"] = ["not text"]
        assert "expected_output" in validate_decomposition([item])

    def test_long_dependency_chain_does_not_use_recursion(self):
        count = 1500
        items = [_st(f"step {i}", [i + 2] if i + 1 < count else []) for i in range(count)]
        assert validate_decomposition(items) is None
