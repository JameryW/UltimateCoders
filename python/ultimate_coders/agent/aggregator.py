"""Result aggregation for distributed subtask execution.

Merges results from multiple workers into a consistent final result:
1. File-level three-way merge (using ConflictResolver)
2. LLM synthesis — combine summaries into coherent final output
3. Partial failure handling — accept partial results with degraded output
4. Consistency checks — verify merged files compile/pass tests

ponytail: sequential merge in wave order — upgrade to parallel
merge if waves produce many files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ultimate_coders.agent.conflict import (
    ConflictResolver,
    MergeResult,
    ResolutionTier,
)
from ultimate_coders.agent.types import FileChange, SubtaskResult

logger = logging.getLogger(__name__)


class AggregationStatus(Enum):
    """Status of result aggregation."""
    SUCCESS = "success"
    PARTIAL = "partial"         # some subtasks failed, partial results accepted
    CONFLICT = "conflict"       # file merge conflicts detected
    FAILED = "failed"           # too many failures, cannot aggregate


@dataclass
class AggregatedResult:
    """Final aggregated result from all subtasks in a task."""

    status: AggregationStatus = AggregationStatus.SUCCESS
    summary: str = ""
    merged_files: list[FileChange] = field(default_factory=list)
    conflict_files: list[str] = field(default_factory=list)
    failed_subtasks: list[str] = field(default_factory=list)
    llm_synthesis: str = ""  # LLM-generated synthesis of all results
    verification_passed: bool | None = None  # None = not verified


class ResultAggregator:
    """Aggregate results from multiple subtasks into a consistent result.

    Usage:
        aggregator = ResultAggregator(llm_client=llm)
        result = await aggregator.aggregate(
            subtask_results=[r1, r2, r3],
            base_files={"main.py": "..."},  # original file contents
        )
    """

    def __init__(
        self,
        llm_client: Any = None,
        max_failure_ratio: float = 0.5,
    ) -> None:
        self._resolver = ConflictResolver(llm_client)
        self._llm_client = llm_client
        self._max_failure_ratio = max_failure_ratio

    async def aggregate(
        self,
        subtask_results: list[SubtaskResult],
        base_files: dict[str, str] | None = None,
        verify_command: str | None = None,
    ) -> AggregatedResult:
        """Aggregate subtask results into a final result.

        Steps:
        1. Collect all modified files
        2. For files modified by multiple subtasks, perform three-way merge
        3. Run verification (compile/test) if command provided
        4. Generate LLM synthesis of all summaries
        5. Handle partial failures

        Args:
            subtask_results: Results from all subtasks.
            base_files: Original file contents (for three-way merge base).
            verify_command: Command to run for verification (e.g. "cargo check").

        Returns:
            AggregatedResult with merged files and status.
        """
        if not subtask_results:
            return AggregatedResult(
                status=AggregationStatus.FAILED,
                summary="No results to aggregate",
            )

        # Separate successes and failures
        successes = [r for r in subtask_results if r.success]
        failures = [r for r in subtask_results if not r.success]
        failure_ratio = len(failures) / len(subtask_results) if subtask_results else 0

        # Check if too many failures
        if failure_ratio > self._max_failure_ratio:
            return AggregatedResult(
                status=AggregationStatus.FAILED,
                summary=(
                    f"Too many failures: "
                    f"{len(failures)}/{len(subtask_results)} subtasks failed"
                ),
                failed_subtasks=[r.subtask_id for r in failures],
            )

        # Step 1: Collect modified files, grouped by path
        file_changes: dict[str, list[FileChange]] = {}
        for result in successes:
            for fc in result.modified_files:
                if fc.file_path not in file_changes:
                    file_changes[fc.file_path] = []
                file_changes[fc.file_path].append(fc)

        # Step 2: Merge files modified by multiple subtasks
        merged_files: list[FileChange] = []
        conflict_files: list[str] = []

        for file_path, changes in file_changes.items():
            if len(changes) == 1:
                # Single modifier — no merge needed
                merged_files.append(changes[0])
            else:
                # Multiple modifiers — three-way merge
                merge_result = await self._merge_file(
                    file_path, changes, base_files,
                )
                if merge_result.success:
                    merged_files.append(FileChange(
                        file_path=file_path,
                        change_type=changes[0].change_type,
                        diff=merge_result.merged or "",
                    ))
                else:
                    conflict_files.append(file_path)
                    # Still include the first change as best-effort
                    merged_files.append(changes[0])

        # Step 3: Build summary
        summaries = [r.summary for r in successes if r.summary]
        combined_summary = "\n".join(f"- {s[:200]}" for s in summaries)

        # Step 4: LLM synthesis (if available)
        llm_synthesis = ""
        if self._llm_client and summaries:
            llm_synthesis = await self._synthesize(summaries)

        # Step 5: Determine status
        if conflict_files:
            status = AggregationStatus.CONFLICT
        elif failures:
            status = AggregationStatus.PARTIAL
        else:
            status = AggregationStatus.SUCCESS

        result = AggregatedResult(
            status=status,
            summary=combined_summary,
            merged_files=merged_files,
            conflict_files=conflict_files,
            failed_subtasks=[r.subtask_id for r in failures],
            llm_synthesis=llm_synthesis,
        )

        # Step 6: Verification (if command provided)
        if verify_command:
            result.verification_passed = await self._verify(verify_command)

        return result

    async def _merge_file(
        self,
        file_path: str,
        changes: list[FileChange],
        base_files: dict[str, str] | None,
    ) -> MergeResult:
        """Merge multiple changes to the same file.

        Uses the ConflictResolver's 4-tier pipeline:
        auto_merge → llm_assisted → reassign → human
        """
        base = (base_files or {}).get(file_path, "")

        # ponytail: merge first two changes, then merge result with next, etc.
        # This is O(n) merge passes. For 2-3 changes per file this is fine.
        current = changes[0].diff if changes[0].diff else base
        for i in range(1, len(changes)):
            theirs = changes[i].diff if changes[i].diff else base
            merge_result = self._resolver.resolve(base, current, theirs)
            if not merge_result.success:
                # Escalate through tiers
                for tier in [ResolutionTier.LLM_ASSISTED, ResolutionTier.REASSIGN]:
                    merge_result = self._resolver.resolve(base, current, theirs, tier=tier)
                    if merge_result.success:
                        break
                if not merge_result.success:
                    return merge_result
            current = merge_result.merged or current

        return MergeResult(merged=current, success=True, tier=ResolutionTier.AUTO_MERGE)

    async def _synthesize(self, summaries: list[str]) -> str:
        """Use LLM to synthesize a coherent summary from all subtask results.

        ponytail: single LLM call — upgrade to multi-turn if synthesis
        quality is poor for complex tasks.
        """
        if not self._llm_client:
            return ""


        prompt = (
            "You are a result synthesis agent. Combine the following subtask results "
            "into a single coherent summary. Focus on what was accomplished, any "
            "issues encountered, and the overall outcome.\n\n"
        )
        for i, summary in enumerate(summaries[:10], 1):  # ponytail: max 10 summaries
            prompt += f"## Subtask {i}:\n{summary[:1000]}\n\n"

        prompt += "\nProvide a concise synthesis (max 500 words):"

        try:
            result = await self._llm_client.complete(prompt=prompt, max_tokens=1024)
            if result and hasattr(result, "text"):
                return result.text
            if isinstance(result, str):
                return result
            return str(result) if result else ""
        except Exception as e:
            logger.warning("LLM synthesis failed: %s", e)
            return ""

    async def _verify(self, command: str) -> bool:
        """Run verification command to check merged result.

        ponytail: simple subprocess — upgrade to timeout + retry
        if verification is flaky.
        """
        import asyncio

        try:
            proc = await asyncio.create_subprocess_exec(
                *command.split(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            return proc.returncode == 0
        except Exception as e:
            logger.warning("Verification command failed: %s", e)
            return False
