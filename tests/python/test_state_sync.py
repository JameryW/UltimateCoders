"""Tests for state_sync — FileChangeEvent, ContextInjector, WorkspaceStateMachine.

Pure-data modules (NATS is optional at runtime); these tests cover the
serialization, context-building, and state-transition logic without any
network dependency.
"""

from __future__ import annotations

from ultimate_coders.agent.state_sync import (
    ContextInjector,
    FileChangeEvent,
    FileChangeEventType,
    WorkspaceState,
    WorkspaceStateMachine,
)


class TestFileChangeEvent:
    def test_post_init_auto_generates_timestamp_and_message_id(self):
        ev = FileChangeEvent(
            task_id="t1", subtask_id="s1", worker_id="w1",
            file_path="a.py", change_type=FileChangeEventType.MODIFIED,
        )
        assert ev.timestamp > 0
        assert ev.message_id == f"t1:s1:a.py:{int(ev.timestamp) // 5000}"

    def test_explicit_timestamp_and_message_id_preserved(self):
        ev = FileChangeEvent(
            task_id="t1", subtask_id="s1", file_path="a.py",
            timestamp=12345.0, message_id="custom-id",
        )
        assert ev.timestamp == 12345.0
        assert ev.message_id == "custom-id"

    def test_to_dict_truncates_diff_summary_to_200(self):
        ev = FileChangeEvent(
            task_id="t1", subtask_id="s1", file_path="a.py",
            diff_summary="x" * 500,
        )
        d = ev.to_dict()
        assert len(d["diff_summary"]) == 200
        assert d["change_type"] == "modified"

    def test_from_dict_round_trips(self):
        ev = FileChangeEvent(
            task_id="t1", subtask_id="s1", worker_id="w1", file_path="a.py",
            change_type=FileChangeEventType.CREATED, diff_summary="hi",
        )
        d = ev.to_dict()
        restored = FileChangeEvent.from_dict(d)
        assert restored.task_id == "t1"
        assert restored.worker_id == "w1"
        assert restored.change_type is FileChangeEventType.CREATED
        assert restored.diff_summary == "hi"
        assert restored.message_id == ev.message_id

    def test_from_dict_defaults_on_missing_fields(self):
        restored = FileChangeEvent.from_dict({})
        assert restored.task_id == ""
        assert restored.change_type is FileChangeEventType.MODIFIED

    def test_from_dict_rejects_unknown_change_type(self):
        import pytest

        with pytest.raises(ValueError):
            FileChangeEvent.from_dict({"change_type": "bogus"})


class TestContextInjector:
    def test_empty_dependencies_returns_empty_string(self):
        inj = ContextInjector()
        assert inj.build_context([]) == ""

    def test_no_matching_results_returns_empty_string(self):
        inj = ContextInjector()
        assert inj.build_context(["nope"]) == ""

    def test_build_context_includes_summary_files_findings(self):
        inj = ContextInjector()
        inj.add_result(
            "st-1", summary="Fixed auth bug",
            modified_files=["login.py", "auth.py"],
            key_findings=["token rotation needed"],
            success=True,
        )
        ctx = inj.build_context(["st-1"])
        assert "st-1"[:8] in ctx
        assert "Fixed auth bug" in ctx
        assert "login.py" in ctx
        assert "token rotation needed" in ctx
        assert "✓" in ctx

    def test_failed_subtask_marked_with_cross(self):
        inj = ContextInjector()
        inj.add_result("st-1", summary="failed", success=False)
        ctx = inj.build_context(["st-1"])
        assert "✗" in ctx

    def test_context_truncated_at_max_chars(self):
        inj = ContextInjector(max_context_chars=200)
        inj.add_result("st-1", summary="y" * 1000)
        ctx = inj.build_context(["st-1"])
        assert len(ctx) <= 200
        assert "context truncated" in ctx

    def test_get_file_state_collects_modified_files(self):
        inj = ContextInjector()
        inj.add_result("st-1", modified_files=["a.py", "b.py"])
        inj.add_result("st-2", modified_files=["b.py", "c.py"])
        state = inj.get_file_state(["st-1", "st-2"])
        assert state == {"a.py": "modified", "b.py": "modified", "c.py": "modified"}

    def test_get_file_state_skips_unknown_deps(self):
        inj = ContextInjector()
        inj.add_result("st-1", modified_files=["a.py"])
        state = inj.get_file_state(["st-1", "unknown"])
        assert state == {"a.py": "modified"}

    def test_clear_empties_results(self):
        inj = ContextInjector()
        inj.add_result("st-1", summary="x")
        inj.clear()
        assert inj.build_context(["st-1"]) == ""


class TestWorkspaceStateMachine:
    def test_create_records_entry(self):
        m = WorkspaceStateMachine()
        entry = m.create("ws-1", "st-1", "uc/subtask/st-1")
        assert entry.state is WorkspaceState.CREATED
        assert entry.branch_name == "uc/subtask/st-1"
        assert m.get_state("ws-1") is entry

    def test_valid_transition_chain_to_merged(self):
        m = WorkspaceStateMachine()
        m.create("ws-1", "st-1", "br")
        for new_state in (WorkspaceState.BRANCHED, WorkspaceState.COMMITTED,
                          WorkspaceState.MERGING, WorkspaceState.MERGED):
            entry = m.transition("ws-1", new_state)
            assert entry is not None
            assert entry.state is new_state

    def test_invalid_transition_returns_entry_unchanged(self):
        m = WorkspaceStateMachine()
        m.create("ws-1", "st-1", "br")
        # CREATED → MERGED is invalid (must go BRANCHED → COMMITTED → MERGING first)
        entry = m.transition("ws-1", WorkspaceState.MERGED)
        assert entry is not None
        assert entry.state is WorkspaceState.CREATED  # unchanged

    def test_transition_unknown_workspace_returns_none(self):
        m = WorkspaceStateMachine()
        assert m.transition("nope", WorkspaceState.BRANCHED) is None

    def test_transition_to_merging_records_commit_sha(self):
        m = WorkspaceStateMachine()
        m.create("ws-1", "st-1", "br")
        m.transition("ws-1", WorkspaceState.BRANCHED)
        m.transition("ws-1", WorkspaceState.COMMITTED)
        entry = m.transition("ws-1", WorkspaceState.MERGING, commit_sha="abc123")
        assert entry.commit_sha == "abc123"

    def test_transition_to_conflict_records_conflict_files(self):
        m = WorkspaceStateMachine()
        m.create("ws-1", "st-1", "br")
        for s in (WorkspaceState.BRANCHED, WorkspaceState.COMMITTED, WorkspaceState.MERGING):
            m.transition("ws-1", s)
        entry = m.transition(
            "ws-1", WorkspaceState.CONFLICT, merge_conflict_files=["a.py", "b.py"],
        )
        assert entry.state is WorkspaceState.CONFLICT
        assert entry.merge_conflict_files == ["a.py", "b.py"]

    def test_get_by_subtask_finds_entry(self):
        m = WorkspaceStateMachine()
        m.create("ws-1", "st-1", "br")
        m.create("ws-2", "st-2", "br2")
        assert m.get_by_subtask("st-2") is m.get_state("ws-2")
        assert m.get_by_subtask("missing") is None

    def test_to_dict_serializes_all_entries(self):
        m = WorkspaceStateMachine()
        m.create("ws-1", "st-1", "br")
        d = m.to_dict()
        assert "ws-1" in d
        assert d["ws-1"]["state"] == "created"
        assert d["ws-1"]["branch_name"] == "br"
