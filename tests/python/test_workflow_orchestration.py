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
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskStatus,
    Task,
    WorkflowStep,
    _resolve_agent_config_field,
)
from ultimate_coders.agent.worker import Worker
from ultimate_coders.nats_worker import _dispatch_mode_from_payload


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


# ── _render_step_prompt: {{prev_outputs_json}} / {{stepN.outputs_json}} ──


def test_render_prev_outputs_json_step0_is_empty_object():
    """Step 0 has no predecessor → {{prev_outputs_json}} = "{}"."""
    w = _make_worker()
    rendered = w._render_step_prompt(
        "prior={{prev_outputs_json}}",
        idx=0,
        step_outputs=[],
        prev=None,
        context_block="",
        file_constraints_str="",
    )
    assert rendered == 'prior={}'


def test_render_prev_outputs_json_contains_full_artifact():
    """{{prev_outputs_json}} serializes summary, success, file_changes,
    stderr_tail, tool_calls — everything the next agent needs."""
    w = _make_worker()
    from ultimate_coders.agent.types import ChangeType, FileChange

    prev = AgentOutput(
        summary="wrote main.rs",
        file_changes=[
            FileChange(
                file_path="src/main.rs",
                change_type=ChangeType.MODIFIED,
                diff="@@ -1,3 +1,5 @@",
            ),
            FileChange(
                file_path="src/lib.rs",
                change_type=ChangeType.CREATED,
                diff="pub fn new() {}",
            ),
        ],
        success=True,
        stderr_tail="warning: unused import",
        tool_calls=["Edit", "Bash"],
    )
    rendered = w._render_step_prompt(
        "CR: {{prev_outputs_json}}",
        idx=1,
        step_outputs=[prev],
        prev=prev,
        context_block="",
        file_constraints_str="",
    )
    import json as _json

    # Extract the JSON blob from the rendered string.
    blob = rendered[len("CR: "):]
    data = _json.loads(blob)

    assert data["summary"] == "wrote main.rs"
    assert data["success"] is True
    assert data["stderr_tail"] == "warning: unused import"
    assert data["tool_calls"] == ["Edit", "Bash"]
    assert len(data["file_changes"]) == 2
    assert data["file_changes"][0]["file_path"] == "src/main.rs"
    assert data["file_changes"][0]["change_type"] == "modified"
    assert data["file_changes"][0]["diff"] == "@@ -1,3 +1,5 @@"
    assert data["file_changes"][1]["file_path"] == "src/lib.rs"
    assert data["file_changes"][1]["change_type"] == "created"
    # token_usage must NOT be present (irrelevant to next agent).
    assert "token_usage" not in data


def test_render_step_n_outputs_json_references_earlier_step():
    """{{step0.outputs_json}} serializes step 0's full AgentOutput."""
    w = _make_worker()
    from ultimate_coders.agent.types import ChangeType, FileChange

    step0 = AgentOutput(
        summary="wrote main.rs",
        file_changes=[
            FileChange(file_path="src/main.rs", change_type=ChangeType.MODIFIED, diff="d"),
        ],
        success=True,
        stderr_tail="",
        tool_calls=["Edit"],
    )
    step1 = AgentOutput(
        summary="CR ok", file_changes=[], success=True, stderr_tail="", tool_calls=[]
    )
    rendered = w._render_step_prompt(
        "revise; step0_json={{step0.outputs_json}}",
        idx=2,
        step_outputs=[step0, step1],
        prev=step1,
        context_block="",
        file_constraints_str="",
    )
    import json as _json

    blob = rendered[len("revise; step0_json="):]
    data = _json.loads(blob)
    assert data["summary"] == "wrote main.rs"
    assert data["success"] is True
    assert data["file_changes"][0]["file_path"] == "src/main.rs"
    assert data["file_changes"][0]["change_type"] == "modified"
    assert data["tool_calls"] == ["Edit"]


def test_render_prev_outputs_json_truncates_large_fields():
    """Truncation keeps the JSON blob from blowing the agent's context window."""
    w = _make_worker()
    from ultimate_coders.agent.types import ChangeType, FileChange

    big_summary = "x" * 5000
    big_diff = "d" * 5000
    big_stderr = "e" * 5000
    many_tools = [f"tool{i}" for i in range(200)]
    prev = AgentOutput(
        summary=big_summary,
        file_changes=[
            FileChange(file_path="a.rs", change_type=ChangeType.MODIFIED, diff=big_diff),
        ],
        success=False,
        stderr_tail=big_stderr,
        tool_calls=many_tools,
    )
    rendered = w._render_step_prompt(
        "{{prev_outputs_json}}",
        idx=1,
        step_outputs=[prev],
        prev=prev,
        context_block="",
        file_constraints_str="",
    )
    import json as _json

    data = _json.loads(rendered)
    # summary ≤ 2000 chars
    assert len(data["summary"]) == 2000
    # stderr_tail ≤ 1000 chars
    assert len(data["stderr_tail"]) == 1000
    # per-file diff ≤ 1000 chars
    assert len(data["file_changes"][0]["diff"]) == 1000
    # tool_calls ≤ 50 entries
    assert len(data["tool_calls"]) == 50


def test_render_prev_outputs_json_omits_token_usage():
    """token_usage (cost/billing) is irrelevant to the next agent — omit it."""
    w = _make_worker()
    from ultimate_coders.agent.sandbox import TokenUsage

    prev = AgentOutput(
        summary="ok",
        token_usage=TokenUsage(input_tokens=1000, output_tokens=500, total_cost_usd=0.05),
    )
    rendered = w._render_step_prompt(
        "{{prev_outputs_json}}",
        idx=1,
        step_outputs=[prev],
        prev=prev,
        context_block="",
        file_constraints_str="",
    )
    import json as _json

    data = _json.loads(rendered)
    assert "token_usage" not in data
    assert data["summary"] == "ok"


def test_render_existing_vars_still_work_alongside_json():
    """Backward compat: {{prev_summary}} / {{prev_files}} coexist with JSON."""
    w = _make_worker()
    from ultimate_coders.agent.types import ChangeType, FileChange

    prev = AgentOutput(
        summary="wrote main.rs",
        file_changes=[FileChange(file_path="src/main.rs", change_type=ChangeType.MODIFIED)],
        success=True,
    )
    rendered = w._render_step_prompt(
        "s={{prev_summary}} f={{prev_files}} j={{prev_outputs_json}}",
        idx=1,
        step_outputs=[prev],
        prev=prev,
        context_block="",
        file_constraints_str="",
    )
    assert "s=wrote main.rs" in rendered
    assert "f=src/main.rs" in rendered
    assert '"summary":"wrote main.rs"' in rendered


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


@pytest.mark.asyncio
async def test_execute_steps_emits_progress_events_per_step():
    """Each step emits a start + end subtask_progress event with step metadata."""
    w = _make_worker()
    w._publish_event = AsyncMock()
    w._sandbox_manager.execute = AsyncMock(
        side_effect=[
            AgentOutput(summary="s0", success=True),
            AgentOutput(summary="s1", success=True),
        ]
    )
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
    await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    # 2 steps × (start + end) = 4 progress events.
    assert w._publish_event.await_count == 4
    calls = w._publish_event.await_args_list
    # First call: step 1 started.
    assert calls[0].args[0] == "subtask_progress"
    assert calls[0].kwargs["task_id"] == "t-1"
    assert calls[0].kwargs["subtask_id"] == "st-1"
    assert calls[0].kwargs["data"]["step_index"] == 0
    assert calls[0].kwargs["data"]["step_total"] == 2
    assert calls[0].kwargs["data"]["step_agent"] == "claude-code"
    assert calls[0].kwargs["data"]["step_status"] == "started"
    # Second call: step 1 completed.
    assert calls[1].kwargs["data"]["step_status"] == "completed"
    assert calls[1].kwargs["data"]["step_summary"] == "s0"
    # Third call: step 2 started (codex).
    assert calls[2].kwargs["data"]["step_agent"] == "codex"
    assert calls[2].kwargs["data"]["step_index"] == 1
    # Fourth call: step 2 completed.
    assert calls[3].kwargs["data"]["step_status"] == "completed"


@pytest.mark.asyncio
async def test_emit_step_event_swallows_publish_failures():
    """A NATS publish failure must not propagate out of _emit_step_event."""
    w = _make_worker()
    w._publish_event = AsyncMock(side_effect=RuntimeError("nats down"))
    subtask = Subtask(id="st", parent_id="t", description="d", status=SubtaskStatus.PENDING)
    # Should not raise.
    await w._emit_step_event(subtask, "subtask_progress", phase="x", step_index=0)


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


# ── agent_config wire-format mismatch (Rust agent_config_json vs Python agent_config) ──


def test_workflow_step_from_dict_accepts_agent_config_json_string():
    """Rust serializes step agent_config as `agent_config_json` (JSON string).

    Python must parse it into the dict the adapter expects.
    """
    import json

    s = WorkflowStep.from_dict(
        {
            "agent": "codex",
            "prompt": "CR",
            "agent_config_json": json.dumps({"agent_name": "reviewer", "tools": ["read"]}),
        }
    )
    assert s.agent_config == {"agent_name": "reviewer", "tools": ["read"]}


def test_workflow_step_from_dict_accepts_agent_config_dict():
    """OMP/Python path sends agent_config as a dict — still works."""
    s = WorkflowStep.from_dict(
        {"agent": "codex", "prompt": "CR", "agent_config": {"agent_name": "reviewer"}}
    )
    assert s.agent_config == {"agent_name": "reviewer"}


def test_workflow_step_from_dict_agent_config_json_empty_or_garbage_safe():
    # Empty string → {}
    assert (
        WorkflowStep.from_dict({"agent": "x", "prompt": "p", "agent_config_json": ""}).agent_config
        == {}
    )
    # Malformed JSON → {} (best-effort, never crash)
    assert (
        WorkflowStep.from_dict(
            {"agent": "x", "prompt": "p", "agent_config_json": "{bad"}
        ).agent_config
        == {}
    )
    # Missing entirely → {}
    assert WorkflowStep.from_dict({"agent": "x", "prompt": "p"}).agent_config == {}


def test_subtask_from_dict_accepts_agent_config_json_string():
    """Subtask-level override also arrives as agent_config_json from Rust."""
    import json

    task = Task.from_dict(
        {
            "id": "t1",
            "description": "d",
            "project_id": "p",
            "status": "in_progress",
            "subtasks": [
                {
                    "id": "st1",
                    "parent_id": "t1",
                    "description": "do thing",
                    "status": "pending",
                    "agent_config_json": json.dumps({"agent_name": "coder"}),
                }
            ],
        }
    )
    assert task.subtasks[0].agent_config == {"agent_name": "coder"}


# ── End-to-end wire: Rust NatsSubtaskExecute payload → Python Subtask ──


def test_nats_payload_with_steps_round_trips_to_subtask():
    """Simulate the exact JSON shape Rust's NatsSubtaskExecute serializes.

    Rust sends (serde, snake_case):
      agent_config_json: Option<String>   (JSON string)
      steps: Vec<WorkflowStep>            (each with agent_config_json: Option<String>)

    Python's _handle_subtask_execute builds a Subtask from this dict. We
    replicate that field extraction (same code path) to prove the full
    wire format round-trips: steps survive, and step-level agent_config
    parses from the JSON string.
    """
    import json

    # What Rust would emit over NATS for a claude-code→codex→claude-code chain.
    payload = {
        "task_id": "t-1",
        "subtask_id": "st-1",
        "description": "implement feature X",
        "expected_output": "working code",
        "file_constraints": [],
        "timeout_seconds": 600,
        "dispatch_mode": "PreferRemote",  # Rust serializes enum variant NAME (PascalCase)
        "required_capabilities": [],
        "agent_config_json": json.dumps({"agent_name": "coder"}),
        "steps": [
            {"agent": "claude-code", "prompt": "write X", "agent_config_json": None},
            {
                "agent": "codex",
                "prompt": "CR: {{prev_summary}}",
                "agent_config_json": json.dumps({"agent_name": "reviewer"}),
            },
            {"agent": "claude-code", "prompt": "revise per {{prev_summary}}"},
        ],
        "project_id": "proj-1",
    }

    # Mirror _handle_subtask_execute's Subtask construction (same field reads).
    subtask = Subtask(
        id=payload["subtask_id"],
        parent_id=payload["task_id"],
        description=payload["description"],
        status=SubtaskStatus.PENDING,
        assigned_worker="w-1",
        depends_on=payload.get("depends_on", []),
        file_constraints=payload.get("file_constraints", []),
        expected_output=payload.get("expected_output", ""),
        timeout_seconds=payload.get("timeout_seconds", 600),
        dispatch_mode=_dispatch_mode_from_payload(payload.get("dispatch_mode", "prefer_remote")),
        required_capabilities=payload.get("required_capabilities", []),
        agent_config=_resolve_agent_config_field(payload),
        steps=[WorkflowStep.from_dict(s) for s in payload.get("steps", [])],
        project_id=payload.get("project_id", ""),
    )

    # Subtask-level config parsed from JSON string.
    assert subtask.agent_config == {"agent_name": "coder"}
    # All three steps survived, in order, with right agents.
    assert [s.agent for s in subtask.steps] == ["claude-code", "codex", "claude-code"]
    # Step 0: no agent_config_json (None) → {}.
    assert subtask.steps[0].agent_config == {}
    # Step 1: agent_config_json string parsed into dict.
    assert subtask.steps[1].agent_config == {"agent_name": "reviewer"}
    # Step 2: key absent entirely → {}.
    assert subtask.steps[2].agent_config == {}
    # Prompt templates preserved verbatim (rendering happens at execute time).
    assert subtask.steps[1].prompt == "CR: {{prev_summary}}"


def test_nats_payload_empty_steps_yields_empty_list():
    """Backward compat: a legacy single-agent subtask sends no `steps` key."""
    payload = {"task_id": "t", "subtask_id": "st", "description": "d"}
    subtask = Subtask(
        id=payload["subtask_id"],
        parent_id=payload["task_id"],
        description=payload["description"],
        status=SubtaskStatus.PENDING,
        steps=[WorkflowStep.from_dict(s) for s in payload.get("steps", [])],
    )
    assert subtask.steps == []


# ── dispatch_mode wire mismatch (Rust PascalCase vs Python lowercase) ──


def test_dispatch_mode_from_payload_accepts_rust_pascalcase():
    from ultimate_coders.agent.types import DispatchMode

    assert _dispatch_mode_from_payload("PreferRemote") is DispatchMode.PREFER_REMOTE
    assert _dispatch_mode_from_payload("Remote") is DispatchMode.REMOTE
    assert _dispatch_mode_from_payload("Local") is DispatchMode.LOCAL


def test_dispatch_mode_from_payload_accepts_python_lowercase():
    from ultimate_coders.agent.types import DispatchMode

    assert _dispatch_mode_from_payload("prefer_remote") is DispatchMode.PREFER_REMOTE
    assert _dispatch_mode_from_payload("remote") is DispatchMode.REMOTE


def test_dispatch_mode_from_payload_defaults_on_bad_or_missing():
    from ultimate_coders.agent.types import DispatchMode

    assert _dispatch_mode_from_payload(None) is DispatchMode.PREFER_REMOTE
    assert _dispatch_mode_from_payload("") is DispatchMode.PREFER_REMOTE
    assert _dispatch_mode_from_payload("garbage") is DispatchMode.PREFER_REMOTE
    assert _dispatch_mode_from_payload(123) is DispatchMode.PREFER_REMOTE


# ── Orchestrator.pending_task_count ──────────────────────────────


def test_pending_task_count_counts_in_progress_not_created():
    """Tasks submit as IN_PROGRESS (never CREATED); pending = active tasks.

    Regression: the old filter counted TaskStatus.CREATED (always 0),
    so the dashboard reported zero pending tasks forever.
    """
    from ultimate_coders.agent.orchestrator import Orchestrator
    from ultimate_coders.agent.types import TaskStatus

    orch = Orchestrator()
    # No tasks → 0.
    assert orch.pending_task_count == 0

    # One IN_PROGRESS task → counted.
    t1 = Task(
        id="t1", description="d", project_id="p",
        status=TaskStatus.IN_PROGRESS, subtasks=[],
    )
    orch.tasks[t1.id] = t1
    assert orch.pending_task_count == 1

    # A COMPLETED task is terminal → not pending.
    t2 = Task(
        id="t2", description="d", project_id="p",
        status=TaskStatus.COMPLETED, subtasks=[],
    )
    orch.tasks[t2.id] = t2
    assert orch.pending_task_count == 1  # only t1

    # A PAUSED task is still active → pending.
    t3 = Task(
        id="t3", description="d", project_id="p",
        status=TaskStatus.PAUSED, subtasks=[],
    )
    orch.tasks[t3.id] = t3
    assert orch.pending_task_count == 2  # t1 + t3


# ── Step retry (retry_count / retry_delay_ms) ────────────────────


def test_workflow_step_retry_fields_roundtrip():
    """retry_count + retry_delay_ms survive to_dict / from_dict round-trip."""
    s = WorkflowStep(agent="claude-code", prompt="flaky", retry_count=3, retry_delay_ms=5000)
    d = s.to_dict()
    assert d["retry_count"] == 3
    assert d["retry_delay_ms"] == 5000
    again = WorkflowStep.from_dict(d)
    assert again.retry_count == 3
    assert again.retry_delay_ms == 5000


def test_workflow_step_retry_defaults_zero():
    """Absent retry fields default to 0 (no retry — backward compat)."""
    s = WorkflowStep.from_dict({"agent": "codex", "prompt": "CR"})
    assert s.retry_count == 0
    assert s.retry_delay_ms == 0


@pytest.mark.asyncio
async def test_execute_steps_retries_failing_step_then_succeeds():
    """retry_count=2 → 3 total attempts; fails twice, succeeds on 3rd."""
    w = _make_worker()
    w._publish_event = AsyncMock()
    # Fail, fail, succeed.
    seq = [
        AgentOutput(summary="fail1", success=False),
        AgentOutput(summary="fail2", success=False),
        AgentOutput(summary="ok", success=True),
    ]
    w._sandbox_manager.execute = AsyncMock(side_effect=seq)
    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="d",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="flaky", retry_count=2, retry_delay_ms=0),
        ],
    )
    out = await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    assert out.success is True
    assert out.summary == "ok"
    # 3 calls total (1 initial + 2 retries).
    assert w._sandbox_manager.execute.await_count == 3


@pytest.mark.asyncio
async def test_execute_steps_retry_exhausted_all_attempts_fail():
    """retry_count=1 → 2 total attempts; both fail → step fails."""
    w = _make_worker()
    w._publish_event = AsyncMock()
    w._sandbox_manager.execute = AsyncMock(
        return_value=AgentOutput(summary="boom", success=False)
    )
    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="d",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="always fails", retry_count=1),
        ],
    )
    out = await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    assert out.success is False
    # 2 calls (initial + 1 retry).
    assert w._sandbox_manager.execute.await_count == 2


@pytest.mark.asyncio
async def test_execute_steps_retry_count_zero_no_retry():
    """retry_count=0 → single attempt on failure (current behavior)."""
    w = _make_worker()
    w._publish_event = AsyncMock()
    w._sandbox_manager.execute = AsyncMock(
        return_value=AgentOutput(summary="fail", success=False)
    )
    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="d",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="fail once", retry_count=0),
        ],
    )
    out = await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    assert out.success is False
    assert w._sandbox_manager.execute.await_count == 1


@pytest.mark.asyncio
async def test_execute_steps_emits_retrying_event():
    """A retry emits step_status='retrying' with retry_attempt (1-indexed)."""
    w = _make_worker()
    w._publish_event = AsyncMock()
    # Fail once, then succeed.
    w._sandbox_manager.execute = AsyncMock(
        side_effect=[
            AgentOutput(summary="fail", success=False),
            AgentOutput(summary="ok", success=True),
        ]
    )
    subtask = Subtask(
        id="st-1",
        parent_id="t-1",
        description="d",
        status=SubtaskStatus.PENDING,
        steps=[
            WorkflowStep(agent="claude-code", prompt="flaky", retry_count=1, retry_delay_ms=0),
        ],
    )
    await w._execute_steps(subtask, working_dir=None, on_stdout_line=None, context_block="")

    calls = w._publish_event.await_args_list
    # Events: started, retrying, completed = 3.
    statuses = [c.kwargs["data"]["step_status"] for c in calls]
    assert statuses == ["started", "retrying", "completed"]
    # The retrying event carries retry_attempt=1.
    retrying_call = calls[1]
    assert retrying_call.kwargs["data"]["retry_attempt"] == 1

