"""Tests for ResultAggregator — distributed subtask result aggregation.

Covers the pure-data paths (no LLM, no real subprocess where avoidable):
empty input, failure-ratio gating, single/multi modifier file handling,
three-way auto-merge success and conflict, partial-failure status, and
verify_command outcome. The LLM synthesis path is exercised via a stub.
"""

from __future__ import annotations

import asyncio

from ultimate_coders.agent.aggregator import (
    AggregationStatus,
    ResultAggregator,
)
from ultimate_coders.agent.types import (
    AdaptationStrategy,
    ChangeType,
    FileChange,
    SubtaskResult,
)


def _result(
    subtask_id: str,
    files: list[FileChange] | None = None,
    summary: str = "",
    success: bool = True,
) -> SubtaskResult:
    return SubtaskResult(
        subtask_id=subtask_id,
        modified_files=files or [],
        summary=summary,
        success=success,
        adaptation_strategy=AdaptationStrategy.NONE,
    )


def _change(path: str, diff: str = "") -> FileChange:
    return FileChange(file_path=path, change_type=ChangeType.MODIFIED, diff=diff)


class TestAggregatorStatusGating:
    async def test_empty_results_returns_failed(self):
        agg = ResultAggregator()
        result = await agg.aggregate([])
        assert result.status is AggregationStatus.FAILED
        assert "No results" in result.summary

    async def test_failure_ratio_exceeds_threshold_returns_failed(self):
        # 2 of 3 failed → 0.67 > default 0.5
        agg = ResultAggregator(max_failure_ratio=0.5)
        results = [
            _result("s1", [_change("a.py", "x")], "ok", success=True),
            _result("s2", success=False, summary="boom"),
            _result("s3", success=False, summary="boom2"),
        ]
        result = await agg.aggregate(results)
        assert result.status is AggregationStatus.FAILED
        assert set(result.failed_subtasks) == {"s2", "s3"}


class TestAggregatorFileMerge:
    async def test_single_modifier_file_passed_through(self):
        agg = ResultAggregator()
        fc = _change("src/main.py", "diff content")
        result = await agg.aggregate([_result("s1", [fc], "did it")])
        assert result.status is AggregationStatus.SUCCESS
        assert result.merged_files == [fc]
        assert "did it" in result.summary

    async def test_disjoint_files_no_merge_needed(self):
        agg = ResultAggregator()
        a, b = _change("a.py", "da"), _change("b.py", "db")
        result = await agg.aggregate([
            _result("s1", [a], "a"),
            _result("s2", [b], "b"),
        ])
        assert result.status is AggregationStatus.SUCCESS
        assert len(result.merged_files) == 2
        assert {fc.file_path for fc in result.merged_files} == {"a.py", "b.py"}

    async def test_same_file_non_overlapping_changes_auto_merge(self):
        # Two workers edit different regions of the same base → auto-merge ok.
        base = "line1\nline2\nline3\nline4\n"
        agg = ResultAggregator()
        ours = "line1\nLINE2\nline3\nline4\n"      # changed line 2
        theirs = "line1\nline2\nline3\nLINE4\n"    # changed line 4
        result = await agg.aggregate(
            [
                _result("s1", [_change("f.py", ours)], "s1"),
                _result("s2", [_change("f.py", theirs)], "s2"),
            ],
            base_files={"f.py": base},
        )
        assert result.status is AggregationStatus.SUCCESS
        assert result.conflict_files == []
        merged = result.merged_files[0].diff
        assert "LINE2" in merged and "LINE4" in merged

    async def test_same_file_overlapping_changes_yield_conflict(self):
        # Both workers change the same line differently → conflict.
        base = "line1\nline2\nline3\n"
        agg = ResultAggregator()
        ours = "line1\nOURS\nline3\n"
        theirs = "line1\nTHEIRS\nline3\n"
        result = await agg.aggregate(
            [
                _result("s1", [_change("f.py", ours)], "s1"),
                _result("s2", [_change("f.py", theirs)], "s2"),
            ],
            base_files={"f.py": base},
        )
        assert result.status is AggregationStatus.CONFLICT
        assert "f.py" in result.conflict_files


class TestAggregatorPartialAndVerify:
    async def test_partial_failure_under_threshold(self):
        # 1 of 3 failed → 0.33 < 0.5 → PARTIAL
        agg = ResultAggregator(max_failure_ratio=0.5)
        result = await agg.aggregate([
            _result("s1", [_change("a.py", "x")], "ok"),
            _result("s2", [_change("b.py", "y")], "ok"),
            _result("s3", success=False, summary="nope"),
        ])
        assert result.status is AggregationStatus.PARTIAL
        assert result.failed_subtasks == ["s3"]

    async def test_verify_command_success_marks_verified(self):
        agg = ResultAggregator()
        result = await agg.aggregate(
            [_result("s1", [_change("a.py", "x")], "ok")],
            verify_command="true",
        )
        assert result.verification_passed is True

    async def test_verify_command_failure_marks_unverified(self):
        agg = ResultAggregator()
        result = await agg.aggregate(
            [_result("s1", [_change("a.py", "x")], "ok")],
            verify_command="false",
        )
        assert result.verification_passed is False

    async def test_no_verify_command_leaves_verification_none(self):
        agg = ResultAggregator()
        result = await agg.aggregate([_result("s1", [_change("a.py", "x")], "ok")])
        assert result.verification_passed is None


class TestAggregatorLLMSynthesis:
    async def test_llm_synthesis_called_when_client_present(self):
        calls: list[str] = []

        class _StubLLM:
            async def complete(self, *, prompt: str, max_tokens: int = 0) -> str:
                calls.append(prompt)
                return "SYNTHESIS"

        agg = ResultAggregator(llm_client=_StubLLM())
        result = await agg.aggregate([
            _result("s1", [_change("a.py", "x")], "did A"),
            _result("s2", [_change("b.py", "y")], "did B"),
        ])
        assert result.llm_synthesis == "SYNTHESIS"
        assert calls and "did A" in calls[0] and "did B" in calls[0]

    async def test_no_llm_client_leaves_synthesis_empty(self):
        agg = ResultAggregator(llm_client=None)
        result = await agg.aggregate([_result("s1", [_change("a.py", "x")], "ok")])
        assert result.llm_synthesis == ""
