"""Conflict detection and resolution for multi-agent code editing.

Uses intent-based locking: workers declare edit intents before modifying
files. When overlapping edits are detected, a tiered resolution pipeline
is applied:

1. Auto-merge (three-way diff) — ~70% success rate
2. LLM-assisted merge — ~90% success rate
3. Re-assign to single worker — ~98% success rate
4. Human escalation — last resort
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class EditType(Enum):
    """Type of edit operation."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class ResolutionTier(Enum):
    """Resolution tier for conflicts."""

    AUTO_MERGE = "auto_merge"
    LLM_ASSISTED = "llm_assisted"
    REASSIGN = "reassign"
    HUMAN = "human"

    def escalate(self) -> ResolutionTier:
        """Get the next tier up (more expensive resolution)."""
        escalation = {
            ResolutionTier.AUTO_MERGE: ResolutionTier.LLM_ASSISTED,
            ResolutionTier.LLM_ASSISTED: ResolutionTier.REASSIGN,
            ResolutionTier.REASSIGN: ResolutionTier.HUMAN,
            ResolutionTier.HUMAN: ResolutionTier.HUMAN,
        }
        return escalation[self]


class ConflictResult(Enum):
    """Result of a conflict check."""

    NO_CONFLICT = "no_conflict"
    POTENTIAL_CONFLICT = "potential_conflict"
    CONFLICTING = "conflicting"


@dataclass
class LineRange:
    """A line range within a file (inclusive start, exclusive end).

    Attributes:
        start: Start line number (1-indexed, inclusive).
        end: End line number (1-indexed, exclusive).
    """

    start: int = 0
    end: int = 0

    def overlaps(self, other: LineRange) -> bool:
        """Check if two line ranges overlap."""
        return self.start < other.end and other.start < self.end


@dataclass
class EditIntent:
    """A declared intent to edit a file.

    Workers should create an EditIntent before modifying a file
    and register it with the ConflictDetector.

    Attributes:
        worker_id: The worker declaring the intent.
        file_path: Path to the file being edited.
        edit_type: Type of edit (create, modify, delete).
        regions: Line ranges being affected.
        timestamp: Unix timestamp (milliseconds).
    """

    worker_id: str = ""
    file_path: str = ""
    edit_type: EditType = EditType.MODIFY
    regions: list[LineRange] = field(default_factory=list)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        import time
        if self.timestamp == 0.0:
            self.timestamp = time.time() * 1000


@dataclass
class ConflictInfo:
    """Information about a detected conflict.

    Attributes:
        file_path: The file with the conflict.
        conflicting_workers: Workers involved in the conflict.
        resolution_tier: The recommended resolution tier.
        overlapping_regions: The overlapping line ranges.
    """

    file_path: str = ""
    conflicting_workers: list[str] = field(default_factory=list)
    resolution_tier: ResolutionTier = ResolutionTier.AUTO_MERGE
    overlapping_regions: list[LineRange] = field(default_factory=list)


@dataclass
class ConflictMarker:
    """A conflict marker for a region that couldn't be auto-merged.

    Attributes:
        start_line: Line number where the conflict starts.
        end_line: Line number where the conflict ends.
        ours: Content from "ours" side.
        theirs: Content from "theirs" side.
        base: Content from the original.
    """

    start_line: int = 0
    end_line: int = 0
    ours: str = ""
    theirs: str = ""
    base: str = ""


@dataclass
class MergeResult:
    """Result of a merge attempt.

    Attributes:
        merged: The merged content, if available.
        conflicts: Conflict markers for regions that couldn't be auto-merged.
        success: Whether the merge was fully successful.
        tier: The resolution tier that was applied.
    """

    merged: str | None = None
    conflicts: list[ConflictMarker] = field(default_factory=list)
    success: bool = False
    tier: ResolutionTier = ResolutionTier.AUTO_MERGE


class ConflictDetector:
    """Intent-based conflict detector.

    Workers declare edit intents before modifying files. The detector
    checks for overlapping regions and returns conflict information.

    Usage:
        detector = ConflictDetector()
        intent = EditIntent(
            worker_id="worker-1",
            file_path="src/main.rs",
            edit_type=EditType.MODIFY,
            regions=[LineRange(start=1, end=20)],
        )
        result = detector.declare_intent(intent)
        if result != ConflictResult.NO_CONFLICT:
            # Handle conflict
        ...
        detector.remove_intent("src/main.rs", "worker-1")
    """

    def __init__(self) -> None:
        self._baseline_hashes: dict[str, str] = {}
        self._active_intents: dict[str, list[EditIntent]] = {}

    def register_baseline(self, file_path: str, hash_value: str) -> None:
        """Register the baseline hash for a file at task start."""
        self._baseline_hashes[file_path] = hash_value

    def declare_intent(self, intent: EditIntent) -> tuple[ConflictResult, ConflictInfo | None]:
        """Declare an edit intent and check for conflicts.

        Args:
            intent: The edit intent to declare.

        Returns:
            A tuple of (ConflictResult, optional ConflictInfo).
        """
        result, info = self.check_conflict(
            intent.file_path, intent.worker_id, intent.regions,
        )

        # Always record the intent
        if intent.file_path not in self._active_intents:
            self._active_intents[intent.file_path] = []
        self._active_intents[intent.file_path].append(intent)

        return result, info

    def check_conflict(
        self,
        file_path: str,
        worker_id: str,
        regions: list[LineRange],
    ) -> tuple[ConflictResult, ConflictInfo | None]:
        """Check if an edit would conflict with existing intents.

        Does NOT record the intent; use declare_intent for that.

        Args:
            file_path: Path to the file being edited.
            worker_id: The worker making the edit.
            regions: Line ranges being affected.

        Returns:
            A tuple of (ConflictResult, optional ConflictInfo).
        """
        existing = self._active_intents.get(file_path, [])
        if not existing:
            return ConflictResult.NO_CONFLICT, None

        conflicting_workers: list[str] = []
        overlapping_regions: list[LineRange] = []

        for intent in existing:
            if intent.worker_id == worker_id:
                continue

            # Check if any regions overlap
            has_overlap = any(
                er.overlaps(nr)
                for er in intent.regions
                for nr in regions
            )

            # Whole-file edit conflict
            whole_file_conflict = (
                (not intent.regions or not regions) and not has_overlap
            )

            if has_overlap or whole_file_conflict:
                if intent.worker_id not in conflicting_workers:
                    conflicting_workers.append(intent.worker_id)
                # Collect overlapping regions
                for er in intent.regions:
                    for nr in regions:
                        if er.overlaps(nr):
                            overlapping_regions.append(LineRange(
                                start=min(er.start, nr.start),
                                end=max(er.end, nr.end),
                            ))

        if not conflicting_workers:
            # Check for other workers on same file
            other_workers = [
                i.worker_id for i in existing if i.worker_id != worker_id
            ]
            if other_workers:
                return ConflictResult.POTENTIAL_CONFLICT, ConflictInfo(
                    file_path=file_path,
                    conflicting_workers=other_workers,
                    resolution_tier=ResolutionTier.AUTO_MERGE,
                )
            return ConflictResult.NO_CONFLICT, None

        return ConflictResult.CONFLICTING, ConflictInfo(
            file_path=file_path,
            conflicting_workers=conflicting_workers,
            resolution_tier=ResolutionTier.AUTO_MERGE,
            overlapping_regions=overlapping_regions,
        )

    def remove_intent(self, file_path: str, worker_id: str) -> None:
        """Remove an intent after the edit is completed or abandoned."""
        if file_path in self._active_intents:
            self._active_intents[file_path] = [
                i for i in self._active_intents[file_path]
                if i.worker_id != worker_id
            ]

    def clear_intents(self) -> None:
        """Clear all intents (e.g., when a task completes)."""
        self._active_intents.clear()

    def get_intents(self, file_path: str) -> list[EditIntent]:
        """Get all active intents for a file."""
        return list(self._active_intents.get(file_path, []))

    def is_file_modified(self, file_path: str, current_hash: str) -> bool:
        """Check if a file's current hash differs from the baseline."""
        baseline = self._baseline_hashes.get(file_path)
        if baseline is None:
            return False
        return baseline != current_hash


class ConflictResolver:
    """4-tier conflict resolution pipeline.

    Tiers:
        1. Auto-merge: Three-way diff merge
        2. LLM-assisted: Use LLM to merge conflicting regions
        3. Reassign: Combine both workers' context into one subtask
        4. Human: Escalate for manual resolution
    """

    def __init__(self, llm_client: object = None) -> None:
        self._llm_client = llm_client

    def resolve(
        self,
        base: str,
        ours: str,
        theirs: str,
        tier: ResolutionTier = ResolutionTier.AUTO_MERGE,
    ) -> MergeResult:
        """Attempt to resolve a conflict at the specified tier.

        Args:
            base: The original (base) content.
            ours: Content from "ours" side.
            theirs: Content from "theirs" side.
            tier: The resolution tier to start with.

        Returns:
            MergeResult with the merged content and any remaining conflicts.
        """
        if tier == ResolutionTier.AUTO_MERGE:
            return self._auto_merge(base, ours, theirs)
        if tier == ResolutionTier.LLM_ASSISTED:
            return self._llm_assisted_merge(base, ours, theirs)
        if tier == ResolutionTier.REASSIGN:
            return self._reassign(base, ours, theirs)
        return MergeResult(
            merged=None,
            success=False,
            tier=ResolutionTier.HUMAN,
        )

    def _auto_merge(self, base: str, ours: str, theirs: str) -> MergeResult:
        """Attempt auto-merge via three-way diff.

        Implements a line-level three-way merge:
        - If only one side changed, take that side.
        - If both sides changed the same lines differently, flag as conflict.
        - If both sides changed different lines, merge both changes.
        """
        base_lines = base.splitlines(keepends=True)
        ours_lines = ours.splitlines(keepends=True)
        theirs_lines = theirs.splitlines(keepends=True)

        # If only one side changed, take that side
        if ours == base:
            return MergeResult(merged=theirs, success=True, tier=ResolutionTier.AUTO_MERGE)
        if theirs == base:
            return MergeResult(merged=ours, success=True, tier=ResolutionTier.AUTO_MERGE)

        # If both sides are identical, no conflict
        if ours == theirs:
            return MergeResult(merged=ours, success=True, tier=ResolutionTier.AUTO_MERGE)

        # Three-way merge: compute which lines changed from base
        ours_changed = self._compute_changes(base_lines, ours_lines)
        theirs_changed = self._compute_changes(base_lines, theirs_lines)

        # Check for overlapping changes
        conflicts = []
        for o_start, o_end in ours_changed:
            for t_start, t_end in theirs_changed:
                if o_start < t_end and t_start < o_end:
                    conflicts.append(ConflictMarker(
                        start_line=o_start + 1,
                        end_line=max(o_end, t_end),
                        ours="".join(ours_lines[o_start:o_end]) or ours,
                        theirs="".join(theirs_lines[t_start:t_end]) or theirs,
                        base="".join(base_lines[o_start:max(o_end, t_end)]) or base,
                    ))

        if not conflicts:
            # Non-overlapping changes: merge by applying both sets
            merged = self._apply_non_conflicting(
                base_lines, ours_lines, theirs_lines, ours_changed, theirs_changed
            )
            return MergeResult(merged=merged, success=True, tier=ResolutionTier.AUTO_MERGE)

        # Overlapping changes: produce conflict markers
        merged = f"<<<<<<< ours\n{ours}\n=======\n{theirs}\n>>>>>>> theirs"
        return MergeResult(
            merged=merged,
            conflicts=conflicts,
            success=False,
            tier=ResolutionTier.AUTO_MERGE,
        )

    @staticmethod
    def _compute_changes(
        base_lines: list[str], changed_lines: list[str],
    ) -> list[tuple[int, int]]:
        """Compute (start, end) ranges of lines that differ from base.

        Returns list of (start, end) tuples where lines differ.
        """
        changes = []
        i = 0
        max_len = max(len(base_lines), len(changed_lines))
        while i < max_len:
            base_line = base_lines[i] if i < len(base_lines) else None
            changed_line = changed_lines[i] if i < len(changed_lines) else None
            if base_line != changed_line:
                start = i
                while i < max_len:
                    base_line = base_lines[i] if i < len(base_lines) else None
                    changed_line = changed_lines[i] if i < len(changed_lines) else None
                    if base_line == changed_line:
                        break
                    i += 1
                changes.append((start, i))
            else:
                i += 1
        return changes

    @staticmethod
    def _apply_non_conflicting(
        base_lines: list[str],
        ours_lines: list[str],
        theirs_lines: list[str],
        ours_changed: list[tuple[int, int]],
        theirs_changed: list[tuple[int, int]],
    ) -> str:
        """Apply non-overlapping changes from both sides to base."""
        # Collect all change regions with their source
        all_changes: list[tuple[int, int, list[str]]] = []
        for start, end in ours_changed:
            all_changes.append((start, end, ours_lines[start:end]))
        for start, end in theirs_changed:
            all_changes.append((start, end, theirs_lines[start:end]))
        all_changes.sort(key=lambda c: c[0])

        result = []
        base_idx = 0
        for start, end, new_lines in all_changes:
            # Copy unchanged base lines before this change
            while base_idx < start and base_idx < len(base_lines):
                result.append(base_lines[base_idx])
                base_idx += 1
            # Skip replaced base lines
            if base_idx < end:
                base_idx = end
            # Apply the change
            result.extend(new_lines)
        # Copy remaining base lines
        while base_idx < len(base_lines):
            result.append(base_lines[base_idx])
            base_idx += 1
        return "".join(result)

    def _llm_assisted_merge(self, base: str, ours: str, theirs: str) -> MergeResult:
        """Attempt LLM-assisted merge.

        Requires an LLM client to be configured. Falls back to reassign
        if no LLM client is available.
        """
        if self._llm_client is None:
            logger.warning("No LLM client for assisted merge, escalating to reassign")
            return self._reassign(base, ours, theirs)

        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        prompt = (
            "You are a code merge assistant. Merge the two modified versions of a file "
            "into a single, correct version. Resolve all conflicts intelligently.\n\n"
            f"## BASE (original):\n```\n{base}\n```\n\n"
            f"## OURS (one modification):\n```\n{ours}\n```\n\n"
            f"## THEIRS (another modification):\n```\n{theirs}\n```\n\n"
            "Output ONLY the merged file content, no explanations or markers."
        )

        try:
            if loop is not None and loop.is_running():
                # Already in async context — create task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    merged = pool.submit(
                        asyncio.run,
                        self._llm_client.complete(prompt=prompt, max_tokens=8192),
                    ).result()
            else:
                merged = asyncio.run(
                    self._llm_client.complete(prompt=prompt, max_tokens=8192),
                )
            if merged and hasattr(merged, "text"):
                merged_text = merged.text
            elif isinstance(merged, str):
                merged_text = merged
            else:
                merged_text = str(merged) if merged else ""

            if merged_text.strip():
                return MergeResult(
                    merged=merged_text, success=True, tier=ResolutionTier.LLM_ASSISTED
                )
        except Exception as e:
            logger.warning("LLM-assisted merge failed: %s", e)

        return MergeResult(
            merged=None,
            success=False,
            tier=ResolutionTier.LLM_ASSISTED,
        )

    def _reassign(self, base: str, ours: str, theirs: str) -> MergeResult:
        """Mark for reassignment to a single worker with full context."""
        return MergeResult(
            merged=None,
            success=False,
            tier=ResolutionTier.REASSIGN,
        )
