"""Tests for Task / WorkflowStep serialization (to_dict / from_dict).

Covers checkpoint persistence roundtrips in agent/types.py:
  - Task.to_dict / from_dict (shape + roundtrip)
  - WorkflowStep.to_dict / from_dict (shape + roundtrip)
  - verify_command exclusion invariant (#561 — runtime metadata, NOT persistent)
  - edge cases: empty subtasks, None fields, missing optional keys

Note: Subtask and SubtaskResult have NO standalone to_dict/from_dict —
they are serialized inline inside Task.to_dict (subtask dict) and
reconstructed inside Task.from_dict. Their roundtrip is therefore tested
through the Task roundtrip.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ultimate_coders.agent.types import (
    AdaptationStrategy,
    ChangeType,
    DispatchMode,
    FileChange,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkflowStep,
)

# ── Helpers ──────────────────────────────────────────────────────


def _make_step(**overrides) -> WorkflowStep:
    """Build a WorkflowStep with all fields populated."""
    base = dict(
        agent="claude-code",
        prompt="Fix the bug in {{file}}",
        agent_config={"tools": ["Edit", "Read"]},
        abort_on_failure=True,
        retry_count=2,
        retry_delay_ms=500,
        condition="prev.success",
        parallel_group="",
    )
    base.update(overrides)
    return WorkflowStep(**base)


def _make_file_change(**overrides) -> FileChange:
    base = dict(
        file_path="src/main.py",
        change_type=ChangeType.MODIFIED,
        diff="@@ -1,3 +1,3 @@\n-old\n+new\n",
    )
    base.update(overrides)
    return FileChange(**base)


def _make_result(**overrides) -> SubtaskResult:
    base = dict(
        subtask_id="st-1",
        worker_id="worker-1",
        modified_files=[_make_file_change()],
        summary="Fixed the bug",
        success=True,
        completed_at=datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        adaptation_strategy=AdaptationStrategy.NONE,
        stderr_tail="",
        recent_tool_calls=["Edit", "Read"],
        retry_count=0,
        error="",
    )
    base.update(overrides)
    return SubtaskResult(**base)


def _make_subtask(**overrides) -> Subtask:
    """Build a Subtask with all serializable fields populated."""
    base = dict(
        id="st-1",
        parent_id="task-1",
        description="Fix the login bug",
        status=SubtaskStatus.COMPLETED,
        assigned_worker="worker-1",
        depends_on=["st-0"],
        priority=5,
        file_constraints=["src/auth.py"],
        expected_output="Login works",
        retry_count=1,
        timeout_seconds=300,
        dispatch_mode=DispatchMode.REMOTE,
        dispatch_retry_count=0,
        agent_config={"agent_name": "claude-code"},
        steps=[_make_step()],
        result=_make_result(),
    )
    base.update(overrides)
    return Subtask(**base)


def _make_task(**overrides) -> Task:
    """Build a Task with all fields populated (including verify_command)."""
    base = dict(
        id="task-1",
        description="Fix all bugs",
        project_id="proj-1",
        status=TaskStatus.IN_PROGRESS,
        subtasks=[_make_subtask()],
        result=None,
        created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 15, 11, 0, 0, tzinfo=timezone.utc),
        verify_command="cargo check",
    )
    base.update(overrides)
    return Task(**base)


# ── WorkflowStep.to_dict ─────────────────────────────────────────


class TestWorkflowStepToDict:
    def test_returns_all_fields(self):
        step = _make_step()
        d = step.to_dict()
        assert d["agent"] == "claude-code"
        assert d["prompt"] == "Fix the bug in {{file}}"
        assert d["agent_config"] == {"tools": ["Edit", "Read"]}
        assert d["abort_on_failure"] is True
        assert d["retry_count"] == 2
        assert d["retry_delay_ms"] == 500
        assert d["condition"] == "prev.success"
        assert d["parallel_group"] == ""

    def test_dict_has_exactly_8_keys(self):
        """to_dict must produce a stable shape — no extra/missing keys."""
        d = _make_step().to_dict()
        assert set(d.keys()) == {
            "agent", "prompt", "agent_config", "abort_on_failure",
            "retry_count", "retry_delay_ms", "condition", "parallel_group",
        }

    def test_defaults(self):
        d = WorkflowStep().to_dict()
        assert d["agent"] == ""
        assert d["prompt"] == ""
        assert d["agent_config"] == {}
        assert d["abort_on_failure"] is True
        assert d["retry_count"] == 0
        assert d["retry_delay_ms"] == 0
        assert d["condition"] == ""
        assert d["parallel_group"] == ""


# ── WorkflowStep.from_dict ───────────────────────────────────────


class TestWorkflowStepFromDict:
    def test_roundtrip(self):
        original = _make_step()
        restored = WorkflowStep.from_dict(original.to_dict())
        assert restored.agent == original.agent
        assert restored.prompt == original.prompt
        assert restored.agent_config == original.agent_config
        assert restored.abort_on_failure == original.abort_on_failure
        assert restored.retry_count == original.retry_count
        assert restored.retry_delay_ms == original.retry_delay_ms
        assert restored.condition == original.condition
        assert restored.parallel_group == original.parallel_group

    def test_empty_dict_uses_defaults(self):
        step = WorkflowStep.from_dict({})
        assert step.agent == ""
        assert step.prompt == ""
        assert step.agent_config == {}
        assert step.abort_on_failure is True
        assert step.retry_count == 0
        assert step.retry_delay_ms == 0
        assert step.condition == ""
        assert step.parallel_group == ""

    def test_accepts_agent_config_json_string(self):
        """_resolve_agent_config_field parses a JSON string (Rust wire format)."""
        d = {"agent": "grok-build", "agent_config_json": '{"agent_name": "x"}'}
        step = WorkflowStep.from_dict(d)
        assert step.agent_config == {"agent_name": "x"}

    def test_agent_config_json_empty_string_yields_empty_dict(self):
        step = WorkflowStep.from_dict({"agent_config_json": ""})
        assert step.agent_config == {}

    def test_agent_config_json_invalid_json_yields_empty_dict(self):
        """Malformed JSON must not crash — best-effort fallback to {}."""
        step = WorkflowStep.from_dict({"agent_config_json": "{not json"})
        assert step.agent_config == {}

    def test_agent_config_json_non_object_yields_empty_dict(self):
        """A valid JSON array/string is not a dict → fallback to {}."""
        step = WorkflowStep.from_dict({"agent_config_json": "[1, 2, 3]"})
        assert step.agent_config == {}

    def test_abort_on_failure_false_preserved(self):
        step = WorkflowStep.from_dict({"abort_on_failure": False})
        assert step.abort_on_failure is False

    def test_retry_count_string_coerced_to_int(self):
        """from_dict uses int(data.get(...)) — string numeric coerced."""
        step = WorkflowStep.from_dict({"retry_count": "3", "retry_delay_ms": "100"})
        assert step.retry_count == 3
        assert step.retry_delay_ms == 100

    def test_retry_count_none_defaults_to_zero(self):
        """int(data.get('retry_count', 0) or 0) — None → 0."""
        step = WorkflowStep.from_dict({"retry_count": None})
        assert step.retry_count == 0

    def test_condition_none_becomes_empty_string(self):
        """data.get('condition', '') or '' — None → ''."""
        step = WorkflowStep.from_dict({"condition": None})
        assert step.condition == ""


# ── Task.to_dict ─────────────────────────────────────────────────


class TestTaskToDict:
    def test_returns_all_top_level_fields(self):
        task = _make_task()
        d = task.to_dict()
        assert d["__version"] == 1
        assert d["id"] == "task-1"
        assert d["description"] == "Fix all bugs"
        assert d["project_id"] == "proj-1"
        assert d["status"] == "in_progress"
        assert d["result"] is None
        assert d["created_at"] == "2025-01-15T10:00:00+00:00"
        assert d["updated_at"] == "2025-01-15T11:00:00+00:00"
        assert isinstance(d["subtasks"], list)
        assert len(d["subtasks"]) == 1

    def test_does_not_include_verify_command(self):
        """#561 invariant: verify_command is runtime metadata, NOT serialized.

        It must never appear in the checkpoint dict — if it did, a checkpoint
        restore would silently resurrect a stale verify command.
        """
        task = _make_task(verify_command="cargo check")
        d = task.to_dict()
        assert "verify_command" not in d

    def test_verify_command_none_also_absent(self):
        """Even when verify_command is None, the key must not appear."""
        task = _make_task(verify_command=None)
        d = task.to_dict()
        assert "verify_command" not in d

    def test_subtask_dict_shape(self):
        d = _make_task().to_dict()
        st = d["subtasks"][0]
        assert st["id"] == "st-1"
        assert st["parent_id"] == "task-1"
        assert st["description"] == "Fix the login bug"
        assert st["status"] == "completed"
        assert st["assigned_worker"] == "worker-1"
        assert st["depends_on"] == ["st-0"]
        assert st["priority"] == 5
        assert st["file_constraints"] == ["src/auth.py"]
        assert st["expected_output"] == "Login works"
        assert st["retry_count"] == 1
        assert st["timeout_seconds"] == 300
        assert st["dispatch_mode"] == "remote"
        assert st["dispatch_retry_count"] == 0
        assert st["agent_config"] == {"agent_name": "claude-code"}
        assert isinstance(st["steps"], list)
        assert len(st["steps"]) == 1
        assert st["result"] is not None

    def test_subtask_result_dict_shape(self):
        d = _make_task().to_dict()
        rd = d["subtasks"][0]["result"]
        assert rd["subtask_id"] == "st-1"
        assert rd["worker_id"] == "worker-1"
        assert rd["summary"] == "Fixed the bug"
        assert rd["success"] is True
        assert rd["completed_at"] == "2025-01-15T10:30:00+00:00"
        assert rd["adaptation_strategy"] == "none"
        assert rd["stderr_tail"] == ""
        assert rd["recent_tool_calls"] == ["Edit", "Read"]
        assert rd["retry_count"] == 0
        assert rd["error"] == ""
        assert isinstance(rd["modified_files"], list)
        assert len(rd["modified_files"]) == 1

    def test_modified_files_dict_shape(self):
        """to_dict renames FileChange fields: file_path→path, diff→diff_stats."""
        d = _make_task().to_dict()
        fc = d["subtasks"][0]["result"]["modified_files"][0]
        assert fc["path"] == "src/main.py"
        assert fc["change_type"] == "modified"
        assert fc["diff_stats"] == "@@ -1,3 +1,3 @@\n-old\n+new\n"

    def test_diff_truncated_to_200_chars(self):
        """to_dict truncates diff to 200 chars (diff[:200])."""
        long_diff = "x" * 500
        fc_change = _make_file_change(diff=long_diff)
        task = _make_task(
            subtasks=[_make_subtask(result=_make_result(modified_files=[fc_change]))],
        )
        d = task.to_dict()
        fc = d["subtasks"][0]["result"]["modified_files"][0]
        assert len(fc["diff_stats"]) == 200

    def test_empty_diff_yields_empty_string(self):
        fc_change = _make_file_change(diff="")
        task = _make_task(
            subtasks=[_make_subtask(result=_make_result(modified_files=[fc_change]))],
        )
        d = task.to_dict()
        fc = d["subtasks"][0]["result"]["modified_files"][0]
        assert fc["diff_stats"] == ""

    def test_result_none_serializes_as_null(self):
        task = _make_task(subtasks=[_make_subtask(result=None)])
        d = task.to_dict()
        assert d["subtasks"][0]["result"] is None

    def test_steps_serialized_via_workflow_step_to_dict(self):
        """Subtask steps are serialized via WorkflowStep.to_dict."""
        step = _make_step(agent="grok-build", condition="prev.success")
        task = _make_task(subtasks=[_make_subtask(steps=[step])])
        d = task.to_dict()
        sd = d["subtasks"][0]["steps"][0]
        assert sd["agent"] == "grok-build"
        assert sd["condition"] == "prev.success"


# ── Task.from_dict ───────────────────────────────────────────────


class TestTaskFromDict:
    def test_roundtrip_basic_fields(self):
        original = _make_task()
        restored = Task.from_dict(original.to_dict())
        assert restored.id == original.id
        assert restored.description == original.description
        assert restored.project_id == original.project_id
        assert restored.status == original.status
        assert restored.result == original.result
        assert restored.created_at == original.created_at
        assert restored.updated_at == original.updated_at

    def test_roundtrip_subtask_fields(self):
        restored = Task.from_dict(_make_task().to_dict())
        st = restored.subtasks[0]
        assert st.id == "st-1"
        assert st.parent_id == "task-1"
        assert st.description == "Fix the login bug"
        assert st.status == SubtaskStatus.COMPLETED
        assert st.assigned_worker == "worker-1"
        assert st.depends_on == ["st-0"]
        assert st.priority == 5
        assert st.file_constraints == ["src/auth.py"]
        assert st.expected_output == "Login works"
        assert st.retry_count == 1
        assert st.timeout_seconds == 300
        assert st.dispatch_mode == DispatchMode.REMOTE
        assert st.dispatch_retry_count == 0
        assert st.agent_config == {"agent_name": "claude-code"}
        assert len(st.steps) == 1

    def test_roundtrip_subtask_result(self):
        restored = Task.from_dict(_make_task().to_dict())
        result = restored.subtasks[0].result
        assert result is not None
        assert result.subtask_id == "st-1"
        assert result.worker_id == "worker-1"
        assert result.summary == "Fixed the bug"
        assert result.success is True
        assert result.completed_at == datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert result.adaptation_strategy == AdaptationStrategy.NONE
        assert result.stderr_tail == ""
        assert result.recent_tool_calls == ["Edit", "Read"]
        assert result.retry_count == 0
        assert result.error == ""

    def test_roundtrip_modified_files(self):
        """FileChange roundtrips with field renaming (path→file_path, diff_stats→diff)."""
        restored = Task.from_dict(_make_task().to_dict())
        fc = restored.subtasks[0].result.modified_files[0]
        assert fc.file_path == "src/main.py"
        assert fc.change_type == ChangeType.MODIFIED
        assert fc.diff == "@@ -1,3 +1,3 @@\n-old\n+new\n"

    def test_roundtrip_workflow_step(self):
        """WorkflowStep embedded in subtask roundtrips correctly."""
        original_step = _make_step(
            agent="grok-build",
            retry_count=3,
            condition="prev.success",
            parallel_group="group-a",
        )
        task = _make_task(subtasks=[_make_subtask(steps=[original_step])])
        restored = Task.from_dict(task.to_dict())
        st = restored.subtasks[0].steps[0]
        assert st.agent == "grok-build"
        assert st.retry_count == 3
        assert st.condition == "prev.success"
        assert st.parallel_group == "group-a"

    def test_verify_command_not_restored(self):
        """#561 invariant: from_dict does NOT read verify_command.

        Even if the source dict has verify_command, from_dict ignores it.
        The field stays at its default (None).
        """
        d = _make_task(verify_command="cargo check").to_dict()
        # Simulate a stale checkpoint that somehow has verify_command
        d["verify_command"] = "cargo check"
        restored = Task.from_dict(d)
        assert restored.verify_command is None

    def test_verify_command_none_after_roundtrip(self):
        """After a clean roundtrip, verify_command is None (not restored)."""
        original = _make_task(verify_command="cargo test")
        restored = Task.from_dict(original.to_dict())
        assert restored.verify_command is None


# ── Edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_subtasks_list(self):
        task = _make_task(subtasks=[])
        d = task.to_dict()
        assert d["subtasks"] == []
        restored = Task.from_dict(d)
        assert restored.subtasks == []

    def test_result_none_roundtrip(self):
        task = _make_task(result=None)
        restored = Task.from_dict(task.to_dict())
        assert restored.result is None

    def test_result_string_roundtrip(self):
        task = _make_task(result="All done")
        restored = Task.from_dict(task.to_dict())
        assert restored.result == "All done"

    def test_task_with_no_subtasks_and_no_result(self):
        """Minimal task: just id + description."""
        task = Task(id="t-min", description="minimal")
        d = task.to_dict()
        assert d["subtasks"] == []
        assert d["result"] is None
        restored = Task.from_dict(d)
        assert restored.id == "t-min"
        assert restored.description == "minimal"
        assert restored.subtasks == []

    def test_from_dict_missing_status_defaults_to_created(self):
        d = _make_task().to_dict()
        del d["status"]
        restored = Task.from_dict(d)
        assert restored.status == TaskStatus.CREATED

    def test_from_dict_missing_created_at_keeps_default(self):
        """from_dict only sets created_at if the key exists."""
        d = _make_task().to_dict()
        del d["created_at"]
        restored = Task.from_dict(d)
        # created_at gets the default (datetime.now), not None
        assert isinstance(restored.created_at, datetime)

    def test_from_dict_missing_updated_at_keeps_default(self):
        d = _make_task().to_dict()
        del d["updated_at"]
        restored = Task.from_dict(d)
        assert isinstance(restored.updated_at, datetime)

    def test_from_dict_missing_subtasks_defaults_to_empty(self):
        d = _make_task().to_dict()
        del d["subtasks"]
        restored = Task.from_dict(d)
        assert restored.subtasks == []

    def test_from_dict_missing_result_defaults_to_none(self):
        d = _make_task().to_dict()
        del d["result"]
        restored = Task.from_dict(d)
        assert restored.result is None

    def test_subtask_missing_status_defaults_to_pending(self):
        d = _make_task().to_dict()
        del d["subtasks"][0]["status"]
        restored = Task.from_dict(d)
        assert restored.subtasks[0].status == SubtaskStatus.PENDING

    def test_subtask_missing_dispatch_mode_defaults_to_prefer_remote(self):
        d = _make_task().to_dict()
        del d["subtasks"][0]["dispatch_mode"]
        restored = Task.from_dict(d)
        assert restored.subtasks[0].dispatch_mode == DispatchMode.PREFER_REMOTE

    def test_subtask_missing_result_keeps_none(self):
        """A subtask with result=None in the dict stays result=None."""
        d = _make_task(subtasks=[_make_subtask(result=None)]).to_dict()
        restored = Task.from_dict(d)
        assert restored.subtasks[0].result is None

    def test_subtask_result_missing_modified_files(self):
        """from_dict handles result dict without modified_files key."""
        d = _make_task().to_dict()
        del d["subtasks"][0]["result"]["modified_files"]
        restored = Task.from_dict(d)
        assert restored.subtasks[0].result.modified_files == []

    def test_subtask_result_missing_completed_at(self):
        """from_dict handles result dict without completed_at key."""
        d = _make_task().to_dict()
        del d["subtasks"][0]["result"]["completed_at"]
        restored = Task.from_dict(d)
        # completed_at gets the SubtaskResult default (datetime.now)
        assert isinstance(restored.subtasks[0].result.completed_at, datetime)

    def test_subtask_result_missing_adaptation_strategy_defaults_to_none(self):
        d = _make_task().to_dict()
        del d["subtasks"][0]["result"]["adaptation_strategy"]
        restored = Task.from_dict(d)
        assert restored.subtasks[0].result.adaptation_strategy == AdaptationStrategy.NONE

    def test_subtask_result_missing_success_defaults_to_true(self):
        d = _make_task().to_dict()
        del d["subtasks"][0]["result"]["success"]
        restored = Task.from_dict(d)
        assert restored.subtasks[0].result.success is True

    def test_multiple_subtasks_roundtrip(self):
        st1 = _make_subtask(id="st-1", description="first")
        st2 = _make_subtask(
            id="st-2", description="second", status=SubtaskStatus.PENDING,
            assigned_worker=None, result=None, depends_on=["st-1"],
        )
        task = _make_task(subtasks=[st1, st2])
        restored = Task.from_dict(task.to_dict())
        assert len(restored.subtasks) == 2
        assert restored.subtasks[0].id == "st-1"
        assert restored.subtasks[1].id == "st-2"
        assert restored.subtasks[1].status == SubtaskStatus.PENDING
        assert restored.subtasks[1].assigned_worker is None
        assert restored.subtasks[1].result is None

    def test_subtask_assigned_worker_none_roundtrip(self):
        """assigned_worker=None serializes as JSON null and restores as None."""
        st = _make_subtask(assigned_worker=None)
        task = _make_task(subtasks=[st])
        d = task.to_dict()
        assert d["subtasks"][0]["assigned_worker"] is None
        restored = Task.from_dict(d)
        assert restored.subtasks[0].assigned_worker is None

    def test_all_change_types_roundtrip(self):
        """Every ChangeType enum value survives the roundtrip."""
        for ct in ChangeType:
            task = _make_task(subtasks=[_make_subtask(result=_make_result(
                modified_files=[_make_file_change(change_type=ct)],
            ))])
            restored = Task.from_dict(task.to_dict())
            assert restored.subtasks[0].result.modified_files[0].change_type == ct

    def test_all_dispatch_modes_roundtrip(self):
        """Every DispatchMode enum value survives the roundtrip."""
        for dm in DispatchMode:
            st = _make_subtask(dispatch_mode=dm)
            task = _make_task(subtasks=[st])
            restored = Task.from_dict(task.to_dict())
            assert restored.subtasks[0].dispatch_mode == dm

    def test_all_task_statuses_roundtrip(self):
        """Every TaskStatus enum value survives the roundtrip."""
        for ts in TaskStatus:
            task = _make_task(status=ts)
            restored = Task.from_dict(task.to_dict())
            assert restored.status == ts

    def test_all_subtask_statuses_roundtrip(self):
        """Every SubtaskStatus enum value survives the roundtrip."""
        for ss in SubtaskStatus:
            st = _make_subtask(status=ss)
            task = _make_task(subtasks=[st])
            restored = Task.from_dict(task.to_dict())
            assert restored.subtasks[0].status == ss

    def test_all_adaptation_strategies_roundtrip(self):
        """Every AdaptationStrategy enum value survives the roundtrip."""
        for strat in AdaptationStrategy:
            task = _make_task(subtasks=[_make_subtask(result=_make_result(
                adaptation_strategy=strat,
            ))])
            restored = Task.from_dict(task.to_dict())
            assert restored.subtasks[0].result.adaptation_strategy == strat

    def test_multiple_steps_roundtrip(self):
        """A subtask with multiple workflow steps roundtrips preserving order."""
        steps = [
            _make_step(agent="grok-build", condition=""),
            _make_step(agent="claude-code", condition="prev.success"),
            _make_step(agent="codex", condition="!prev.success", retry_count=2),
        ]
        task = _make_task(subtasks=[_make_subtask(steps=steps)])
        restored = Task.from_dict(task.to_dict())
        assert len(restored.subtasks[0].steps) == 3
        assert restored.subtasks[0].steps[0].agent == "grok-build"
        assert restored.subtasks[0].steps[1].agent == "claude-code"
        assert restored.subtasks[0].steps[2].agent == "codex"
        assert restored.subtasks[0].steps[2].retry_count == 2

    def test_subtask_with_empty_steps(self):
        """A subtask with steps=[] roundtrips as empty list."""
        st = _make_subtask(steps=[])
        task = _make_task(subtasks=[st])
        restored = Task.from_dict(task.to_dict())
        assert restored.subtasks[0].steps == []

    def test_subtask_missing_steps_key_defaults_to_empty(self):
        """from_dict uses sd.get('steps', []) — missing key → []."""
        d = _make_task().to_dict()
        del d["subtasks"][0]["steps"]
        restored = Task.from_dict(d)
        assert restored.subtasks[0].steps == []

    def test_multiple_modified_files_roundtrip(self):
        """Multiple FileChanges survive the roundtrip preserving order."""
        files = [
            _make_file_change(file_path="a.py", change_type=ChangeType.CREATED),
            _make_file_change(file_path="b.py", change_type=ChangeType.MODIFIED),
            _make_file_change(file_path="c.py", change_type=ChangeType.DELETED),
        ]
        task = _make_task(subtasks=[_make_subtask(result=_make_result(
            modified_files=files,
        ))])
        restored = Task.from_dict(task.to_dict())
        rfs = restored.subtasks[0].result.modified_files
        assert len(rfs) == 3
        assert rfs[0].file_path == "a.py"
        assert rfs[0].change_type == ChangeType.CREATED
        assert rfs[1].file_path == "b.py"
        assert rfs[2].file_path == "c.py"
        assert rfs[2].change_type == ChangeType.DELETED

    def test_full_roundtrip_preserves_is_complete(self):
        """A task with all subtasks completed is_complete after roundtrip."""
        task = _make_task(subtasks=[_make_subtask(status=SubtaskStatus.COMPLETED)])
        restored = Task.from_dict(task.to_dict())
        assert restored.is_complete is True

    def test_full_roundtrip_preserves_has_failed(self):
        """A task with a failed subtask has_failed after roundtrip."""
        task = _make_task(subtasks=[_make_subtask(status=SubtaskStatus.FAILED)])
        restored = Task.from_dict(task.to_dict())
        assert restored.has_failed is True

    def test_diff_truncation_is_lossy_on_roundtrip(self):
        """Diff >200 chars is truncated in to_dict, so roundtrip is lossy.

        This documents the known behavior: to_dict stores diff[:200] as
        diff_stats, and from_dict restores it as diff. A long diff does
        NOT survive the roundtrip intact.
        """
        long_diff = "A" * 500
        task = _make_task(subtasks=[_make_subtask(result=_make_result(
            modified_files=[_make_file_change(diff=long_diff)],
        ))])
        restored = Task.from_dict(task.to_dict())
        fc = restored.subtasks[0].result.modified_files[0]
        assert len(fc.diff) == 200  # truncated, not 500
        assert fc.diff == "A" * 200


# ── JSON serializable ────────────────────────────────────────────


class TestJsonSerializable:
    def test_task_to_dict_is_json_serializable(self):
        """The whole point of to_dict is JSON checkpoint persistence."""
        import json
        d = _make_task().to_dict()
        # Must not raise — every value is JSON-native
        s = json.dumps(d)
        assert isinstance(s, str)
        # And parses back to the same structure
        assert json.loads(s) == d

    def test_workflow_step_to_dict_is_json_serializable(self):
        import json
        d = _make_step().to_dict()
        s = json.dumps(d)
        assert json.loads(s) == d

    def test_task_with_failed_result_is_json_serializable(self):
        """A failed subtask result (error, stderr_tail populated) serializes."""
        import json
        task = _make_task(subtasks=[_make_subtask(
            status=SubtaskStatus.FAILED,
            result=_make_result(
                success=False,
                error="Compilation failed",
                stderr_tail="error: expected `;`",
                adaptation_strategy=AdaptationStrategy.SHRINK_SCOPE,
                retry_count=3,
            ),
        )])
        d = task.to_dict()
        s = json.dumps(d)
        restored_data = json.loads(s)
        assert restored_data["subtasks"][0]["result"]["success"] is False
        assert restored_data["subtasks"][0]["result"]["error"] == "Compilation failed"
