"""Tests for conflict detection and resolution."""

from __future__ import annotations

from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictResolver,
    ConflictResult,
    EditIntent,
    EditType,
    LineRange,
    ResolutionTier,
)


class TestConflictDetector:
    """Tests for ConflictDetector."""

    def test_no_conflict_single_worker(self):
        detector = ConflictDetector()
        intent = EditIntent(
            worker_id="w1",
            file_path="main.rs",
            edit_type=EditType.MODIFY,
            regions=[LineRange(start=1, end=20)],
        )
        result, info = detector.declare_intent(intent)
        assert result == ConflictResult.NO_CONFLICT
        assert info is None

    def test_conflict_overlapping_regions(self):
        detector = ConflictDetector()
        detector.declare_intent(EditIntent(
            worker_id="w1", file_path="main.rs",
            regions=[LineRange(start=1, end=20)],
        ))
        result, info = detector.declare_intent(EditIntent(
            worker_id="w2", file_path="main.rs",
            regions=[LineRange(start=10, end=30)],
        ))
        assert result == ConflictResult.CONFLICTING
        assert info is not None
        assert "w1" in info.conflicting_workers

    def test_no_conflict_different_regions(self):
        detector = ConflictDetector()
        detector.declare_intent(EditIntent(
            worker_id="w1", file_path="main.rs",
            regions=[LineRange(start=1, end=10)],
        ))
        result, info = detector.declare_intent(EditIntent(
            worker_id="w2", file_path="main.rs",
            regions=[LineRange(start=20, end=30)],
        ))
        assert result == ConflictResult.POTENTIAL_CONFLICT

    def test_remove_intent(self):
        detector = ConflictDetector()
        detector.declare_intent(EditIntent(
            worker_id="w1", file_path="main.rs",
            regions=[LineRange(start=1, end=20)],
        ))
        detector.remove_intent("main.rs", "w1")
        result, info = detector.declare_intent(EditIntent(
            worker_id="w2", file_path="main.rs",
            regions=[LineRange(start=1, end=20)],
        ))
        assert result == ConflictResult.NO_CONFLICT


class TestAutoMerge:
    """Tests for ConflictResolver._auto_merge (three-way diff)."""

    def setup_method(self):
        self.resolver = ConflictResolver()

    def test_one_side_unchanged(self):
        base = "line1\nline2\nline3"
        ours = "line1\nline2-modified\nline3"
        theirs = base
        result = self.resolver._auto_merge(base, ours, theirs)
        assert result.success
        assert result.merged == ours

    def test_other_side_unchanged(self):
        base = "line1\nline2\nline3"
        ours = base
        theirs = "line1\nline2-modified\nline3"
        result = self.resolver._auto_merge(base, ours, theirs)
        assert result.success
        assert result.merged == theirs

    def test_both_sides_identical(self):
        base = "line1\nline2\nline3"
        ours = "line1\nmodified\nline3"
        result = self.resolver._auto_merge(base, ours, ours)
        assert result.success
        assert result.merged == ours

    def test_non_overlapping_changes(self):
        base = "line1\nline2\nline3\nline4\nline5"
        ours = "line1-ours\nline2\nline3\nline4\nline5"
        theirs = "line1\nline2\nline3\nline4-theirs\nline5"
        result = self.resolver._auto_merge(base, ours, theirs)
        assert result.success
        assert result.merged is not None
        assert "line1-ours" in result.merged
        assert "line4-theirs" in result.merged

    def test_overlapping_changes_conflict(self):
        base = "line1\nline2\nline3"
        ours = "line1-ours\nline2-ours\nline3"
        theirs = "line1-theirs\nline2-theirs\nline3"
        result = self.resolver._auto_merge(base, ours, theirs)
        assert not result.success
        assert result.conflicts


class TestLlmAssistedMerge:
    """Tests for ConflictResolver._llm_assisted_merge."""

    def test_no_llm_client_escalates(self):
        resolver = ConflictResolver(llm_client=None)
        result = resolver._llm_assisted_merge("base", "ours", "theirs")
        assert result.tier == ResolutionTier.REASSIGN
        assert not result.success

    def test_with_mock_llm_client(self):
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "merged content"

        async def _complete(**kwargs):
            return mock_response

        mock_client.complete = _complete
        resolver = ConflictResolver(llm_client=mock_client)
        result = resolver._llm_assisted_merge("base", "ours", "theirs")
        assert result.tier == ResolutionTier.LLM_ASSISTED
        assert result.success
        assert result.merged == "merged content"
