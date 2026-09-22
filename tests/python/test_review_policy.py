"""Explicit review verdicts at the worker/executor boundary."""

from unittest.mock import AsyncMock

import pytest
from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import FileChange, Subtask, WorkflowStep
from ultimate_coders.agent.worker import Worker


async def test_explicit_rejection_is_not_a_successful_execution():
    worker = Worker(worker_id="reviewer")
    worker.MAX_RETRIES = 1
    worker._sandbox_manager.execute = AsyncMock(return_value=AgentOutput(
        summary='{"approved": false, "issues": ["Missing test"], "suggestions": []}',
        success=True,
    ))
    result = await worker.execute_subtask(Subtask(
        id="r", parent_id="g", description="Review committed output",
        required_capabilities=["review"],
    ))
    assert result.success is False
    assert result.review is not None
    assert result.review.approved is False
    assert result.review.issues == ["Missing test"]


@pytest.mark.parametrize("summary,changes,agent_success,approved", [
    ('{"approved": true, "issues": [], "suggestions": []}', [], True, True),
    ('```json\n{"approved": true, "issues": [], "suggestions": []}\n```', [], True, True),
    ('{"approved": "true", "issues": [], "suggestions": []}', [], True, False),
    ('{"approved": true, "issues": [42], "suggestions": []}', [], True, False),
    ('{"approved": true}', [], True, False),
    ('Looks good', [], True, False),
    ('{"approved": true, "issues": [], "suggestions": []}', [FileChange("a.py")], True, False),
    ('{"approved": true, "issues": [], "suggestions": []}', [], False, False),
])
async def test_review_requires_a_valid_successful_read_only_verdict(
    summary, changes, agent_success, approved,
):
    worker = Worker(worker_id="reviewer")
    worker.MAX_RETRIES = 1
    worker._sandbox_manager.execute = AsyncMock(return_value=AgentOutput(
        summary=summary, file_changes=changes, success=agent_success,
    ))
    result = await worker.execute_subtask(Subtask(
        id="r", parent_id="g", description="Inspect", required_capabilities=["review"],
    ))
    assert result.success is approved
    if approved:
        assert result.review is not None and result.review.approved
    else:
        assert result.review is None
        assert result.error
    prompt = worker._sandbox_manager.execute.call_args.args[0]
    assert "without modifying files" in prompt
    assert '"approved"' in prompt


@pytest.mark.parametrize("capability", ["code", "code-review", "Review"])
async def test_normal_nodes_do_not_require_a_json_verdict(capability):
    worker = Worker(worker_id="normal")
    worker._sandbox_manager.execute = AsyncMock(return_value=AgentOutput(summary="done"))
    result = await worker.execute_subtask(Subtask(
        id="n", parent_id="g", description="Review wording", required_capabilities=[capability],
    ))
    assert result.success
    assert result.review is None


async def test_workflow_review_uses_the_same_verdict_contract():
    worker = Worker(worker_id="reviewer")
    worker._sandbox_manager.execute = AsyncMock(return_value=AgentOutput(
        summary='{"approved": true, "issues": [], "suggestions": ["More tests"]}',
    ))
    result = await worker.execute_subtask(Subtask(
        id="r", parent_id="g", description="Inspect", required_capabilities=["review"],
        steps=[WorkflowStep(agent="codex", prompt="Inspect this change")],
    ))
    assert result.success
    assert result.review is not None
    assert result.review.suggestions == ["More tests"]
    prompt = worker._sandbox_manager.execute.call_args.args[0]
    assert "without modifying files" in prompt
    assert '"approved"' in prompt


async def test_review_cannot_hide_a_non_aborting_failed_step():
    worker = Worker(worker_id="reviewer")
    worker.MAX_RETRIES = 1
    calls = 0

    async def execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AgentOutput(
                summary="changed before failure", success=False,
                file_changes=[FileChange("a.py")],
            )
        return AgentOutput(summary='{"approved": true, "issues": [], "suggestions": []}')

    worker._sandbox_manager.execute = AsyncMock(side_effect=execute)
    result = await worker.execute_subtask(Subtask(
        id="r", parent_id="g", description="Inspect", required_capabilities=["review"],
        steps=[
            WorkflowStep(agent="codex", prompt="first", abort_on_failure=False),
            WorkflowStep(agent="codex", prompt="second"),
        ],
    ))
    assert result.success is False
    assert result.review is None
    assert result.error


async def test_checkpoint_replay_preserves_approved_verdict():
    class Memory:
        def __init__(self):
            self.values = {}

        async def read_memory_async(self, **kwargs):
            return self.values.get(kwargs["key"])

        async def write_memory_async(self, **kwargs):
            self.values[kwargs["key"]] = kwargs["content"]

    worker = Worker(worker_id="reviewer")
    worker.engine = Memory()
    worker._sandbox_manager.execute = AsyncMock(return_value=AgentOutput(
        summary='{"approved": true, "issues": [], "suggestions": []}',
    ))
    task = Subtask(id="r", parent_id="g", required_capabilities=["review"])
    first = await worker.execute_subtask(task)
    replay = await worker.execute_subtask(task)
    assert first.review is not None
    assert replay.review == first.review
    assert replay.success
    assert worker._sandbox_manager.execute.await_count == 1
