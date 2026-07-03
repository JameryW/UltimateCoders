"""Tests for subtask multi-agent workflow orchestration.

Covers the step-chain path added in worker.py: a subtask with non-empty
`steps` runs each step's agent in order, threading each step's
AgentOutput into the next step's prompt template, and accumulates file
changes across the whole chain.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import Subtask, SubtaskStatus, WorkflowStep
from ultimate_coders.agent.worker import Worker


def _make_worker() -> Worker:
    """Build a Worker without running heavy __init__ deps.

    We only exercise the pure rendering + the step-chain driver, which
    needs _sandbox_manager.execute and _resolve_agent_config.
    """
    w = Worker.__new__(Worker)
    w._sandbox_manager = MagicMock()
    w._sandbox_manager.execute = AsyncMock()
    w.worker_id = "w-test"
    return w


# ── _render_step_prompt ──────────────────────────────────────────


def test_render_step0_no_prev_resolves_empty():
    w = _make_worker()
    rendered = w._render_step_prompt(
        "do: {{prev_summary}} | {{prev_files}}",
        idx=0,
        step_outputs=[],
        prev=None,
        context_block="",
        file_constraints_str="none",
    )
    assert rendered == "do:  | "


def test_render_threads_prev_summary_and_files():
    w = _make_worker()
    from ultimate_coders.agent.types import ChangeType, FileChange

    prev = AgentOutput(
        summary="wrote main.rs",
        file_changes=[
            FileChange(file_path="src/main.rs", change_type=ChangeType.MODIFIED, diff="")
        ],
    )
    rendered = w._render_step_prompt(
        "CR this: {{prev_summary}}\nfiles:\n{{prev_files}}",
        idx=1,
        step_outputs=[prev],
        prev=prev,
        context_block="",
        file_constraints_str="none",
    )
    assert "CR this: wrote main.rs" in rendered
    assert "src/main.rs" in rendered


def test_render_references_step_index_by_n():
    w = _make_worker()
    outs = [
        AgentOutput(summary="wrote main.rs", file_changes=[]),
        AgentOutput(summary="CR: add tests", file_changes=[]),
    ]
    rendered = w._render_step_prompt(
        "revise; step0={{step0.summary}} step1={{step1.summary}}",
        idx=2,
        step_outputs=outs,
        prev=outs[-1],
        context_block="",
        file_constraints_str="none",
    )
    assert rendered == "revise; step0=wrote main.rs step1=CR: add tests"


def test_render_injects_context_and_file_constraints():
    w = _make_worker()
    rendered = w._render_step_prompt(
        "ctx={{context}} fc={{file_constraints}}",
        idx=0,
        step_outputs=[],
        prev=None,
        context_block="CTX",
        file_constraints_str="a.rs, b.rs",
    )
    assert rendered == "ctx=CTX fc=a.rs, b.rs"


# ── _execute_steps ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_steps_runs_chain_in_order_and_threads_output():
    """claude-code → codex → claude-code: each step sees the prior output."""
    w = _make_worker()
    calls: list[tuple[str, str]] = []  # (agent, prompt)

    async def fake_execute(
        prompt, *, working_dir=None, on_stdout_line=None, subtask_config=None, agent=None
    ):
        calls.append((agent, prompt))
        # Return distinct summaries so we can assert they were threaded.
        if agent == "claude-code" and "write" in prompt:
            return AgentOutput(summary="wrote code", success=True)
        if agent == "codex":
            return AgentOutput(summary="CR: looks good", success=True)
        return AgentOutput(summary="revised", success=True)

    w._sandbox_manager.execute.side_effect = fake_execute

    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="implement feature",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="write the feature"),
            WorkflowStep(agent="codex", prompt="CR this: {{prev_summary}}"),
            WorkflowStep(agent="claude-code", prompt="revise per: {{prev_summary}}"),
        ],
    )

    out = await w._execute_steps(subtask, working_dir="/wd", on_stdout_line=None, context_block="")

    assert [c[0] for c in calls] == ["claude-code", "codex", "claude-code"]
    # Step 1 (codex) should have received step 0's summary.
    assert "wrote code" in calls[1][1]
    # Step 2 (claude-code revise) should have received step 1's summary.
    assert "CR: looks good" in calls[2][1]
    # Final output is the last step's summary.
    assert out.summary == "revised"
    assert out.success is True


@pytest.mark.asyncio
async def test_execute_steps_aborts_on_failure_by_default():
    w = _make_worker()
    w._sandbox_manager.execute = AsyncMock(return_value=AgentOutput(summary="boom", success=False))
    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="d",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="write"),
            WorkflowStep(agent="codex", prompt="cr {{prev_summary}}"),
        ],
    )
    out = await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    assert out.success is False
    assert "step 1" in out.summary and "failed" in out.summary
    # Second step must NOT have run.
    assert w._sandbox_manager.execute.await_count == 1


@pytest.mark.asyncio
async def test_execute_steps_continues_when_abort_on_failure_false():
    w = _make_worker()
    seq = [
        AgentOutput(summary="fail1", success=False),
        AgentOutput(summary="ok2", success=True),
    ]
    w._sandbox_manager.execute = AsyncMock(side_effect=seq)
    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="d",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="s0", abort_on_failure=False),
            WorkflowStep(agent="codex", prompt="s1 {{prev_summary}}"),
        ],
    )
    out = await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    assert w._sandbox_manager.execute.await_count == 2
    # Last step's output wins.
    assert out.summary == "ok2"
    assert out.success is True


@pytest.mark.asyncio
async def test_execute_steps_accumulates_file_changes_across_chain():
    from ultimate_coders.agent.types import ChangeType, FileChange

    w = _make_worker()
    seq = [
        AgentOutput(
            summary="s0",
            file_changes=[
                FileChange(file_path="a.rs", change_type=ChangeType.CREATED, diff=""),
            ],
        ),
        AgentOutput(
            summary="s1",
            file_changes=[
                FileChange(file_path="b.rs", change_type=ChangeType.MODIFIED, diff=""),
            ],
        ),
    ]
    w._sandbox_manager.execute = AsyncMock(side_effect=seq)
    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="d",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="s0"),
            WorkflowStep(agent="codex", prompt="s1"),
        ],
    )
    out = await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    paths = {fc.file_path for fc in out.file_changes}
    assert paths == {"a.rs", "b.rs"}


# ── WorkflowStep serialization ───────────────────────────────────


def test_workflow_step_roundtrip():
    s = WorkflowStep(agent="codex", prompt="CR {{prev_summary}}", abort_on_failure=False)
    s.agent_config = {"agent_name": "reviewer"}
    d = s.to_dict()
    again = WorkflowStep.from_dict(d)
    assert again.agent == "codex"
    assert again.prompt == "CR {{prev_summary}}"
    assert again.abort_on_failure is False
    assert again.agent_config == {"agent_name": "reviewer"}


def test_workflow_step_default_abort_on_failure_true():
    assert WorkflowStep(agent="codex", prompt="x").abort_on_failure is True


def test_subtask_steps_default_empty():
    s = Subtask(id="x", parent_id="p")
    assert s.steps == []
