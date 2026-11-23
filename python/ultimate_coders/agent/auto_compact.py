"""AutoCompact: agent-initiated context compaction with summary auditing (#31).

Ledger item #31 adapts AutoCompact (arXiv:2610.02163).  Fixed-threshold
compaction is tied to context length rather than task phase, and an SFT summary
can contradict itself -- for example it records an unresolved ``SyntaxError``
yet proposes submitting the patch.  This module exposes compaction as an
explicit policy action, audits the working-state summary before accepting it,
and slices the trajectory at compaction boundaries into monotonic segments that
can share one group-normalised advantage.

Feature flag: ``UC_AUTO_COMPACT`` (default off).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "UC_AUTO_COMPACT",
    "CompactTrigger",
    "SummaryAudit",
    "WorkingState",
    "SummaryDraft",
    "CompactionDecision",
    "CompactionResult",
    "TrajectorySegment",
    "AutoCompactPolicy",
    "auto_compact_enabled",
    "build_segments",
]

UC_AUTO_COMPACT = "UC_AUTO_COMPACT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_COMPLETION_VERBS = (
    "submit",
    "finish",
    "complete",
    "done",
    "finalize",
    "declare",
    "ship",
    "wrap up",
    "return the patch",
)
_REPAIR_VERBS = (
    "fix",
    "repair",
    "debug",
    "investigate",
    "rerun",
    "run test",
    "test",
    "inspect",
    "read",
    "verify",
    "patch",
    "edit",
    "analyze",
)


def auto_compact_enabled() -> bool:
    """Return whether agent-initiated compaction is enabled."""

    return os.environ.get(UC_AUTO_COMPACT, "").strip().lower() in _TRUTHY


class CompactTrigger(str, Enum):
    """Why a compaction is being considered."""

    PHASE_COMPLETE = "phase_complete"
    BUDGET_PRESSURE = "budget_pressure"
    MANUAL = "manual"
    ERROR_RECOVERY = "error_recovery"


@dataclass(frozen=True)
class WorkingState:
    """The part of the working state a compaction must preserve."""

    step: int
    phase: str
    used_tokens: int
    budget_tokens: int = 2000
    unresolved_errors: tuple[str, ...] = ()
    resolved_errors: tuple[str, ...] = ()
    files_touched: tuple[str, ...] = ()
    proposed_next_action: str = ""
    completed_phases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "step", max(0, int(self.step)))
        object.__setattr__(self, "phase", str(self.phase).strip())
        object.__setattr__(self, "used_tokens", max(0, int(self.used_tokens)))
        object.__setattr__(self, "budget_tokens", max(1, int(self.budget_tokens)))
        object.__setattr__(
            self, "unresolved_errors", tuple(_clean(value) for value in self.unresolved_errors)
        )
        object.__setattr__(
            self, "resolved_errors", tuple(_clean(value) for value in self.resolved_errors)
        )
        object.__setattr__(
            self, "files_touched", tuple(_clean(value) for value in self.files_touched)
        )
        object.__setattr__(self, "proposed_next_action", str(self.proposed_next_action).strip())
        object.__setattr__(
            self,
            "completed_phases",
            tuple(_clean(value) for value in self.completed_phases),
        )

    @property
    def used_ratio(self) -> float:
        """Fraction of the current window in use, clamped to ``[0, 1]``."""

        return min(1.0, self.used_tokens / self.budget_tokens)

    @property
    def has_unresolved_errors(self) -> bool:
        return bool(self.unresolved_errors)

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "phase": self.phase,
            "used_tokens": self.used_tokens,
            "budget_tokens": self.budget_tokens,
            "used_ratio": round(self.used_ratio, 6),
            "unresolved_errors": list(self.unresolved_errors),
            "resolved_errors": list(self.resolved_errors),
            "files_touched": list(self.files_touched),
            "proposed_next_action": self.proposed_next_action,
            "completed_phases": list(self.completed_phases),
        }


def _clean(value: object) -> str:
    return str(value).strip()


@dataclass(frozen=True)
class SummaryAudit:
    """Result of checking a summary against the preserved working state."""

    violations: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.violations

    @property
    def requires_revision(self) -> bool:
        return not self.consistent

    def to_dict(self) -> dict[str, object]:
        return {
            "consistent": self.consistent,
            "requires_revision": self.requires_revision,
            "violations": list(self.violations),
        }


# Short alias used by callers that keep the working-state vocabulary.
WorkingStateSummary = SummaryAudit


def _looks_like_completion(action: str) -> bool:
    lowered = action.lower()
    if not any(verb in lowered for verb in _COMPLETION_VERBS):
        return False
    return not any(verb in lowered for verb in _REPAIR_VERBS)


@dataclass(frozen=True)
class SummaryDraft:
    """A candidate ``# Auto Context Summary`` and its preserved fields."""

    body: str
    unresolved_errors: tuple[str, ...] = ()
    resolved_errors: tuple[str, ...] = ()
    proposed_next_action: str = ""
    files_touched: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", str(self.body).strip())
        object.__setattr__(
            self, "unresolved_errors", tuple(_clean(value) for value in self.unresolved_errors)
        )
        object.__setattr__(
            self, "resolved_errors", tuple(_clean(value) for value in self.resolved_errors)
        )
        object.__setattr__(self, "proposed_next_action", str(self.proposed_next_action).strip())
        object.__setattr__(
            self, "files_touched", tuple(_clean(value) for value in self.files_touched)
        )

    def audit(self) -> SummaryAudit:
        """Detect contradictions between recorded errors and the next action."""

        violations: list[str] = []
        if not self.body:
            violations.append("empty_summary")
        if not self.proposed_next_action:
            violations.append("missing_next_action")
        unresolved = {value.lower() for value in self.unresolved_errors}
        resolved = {value.lower() for value in self.resolved_errors}
        overlap = unresolved & resolved
        if overlap:
            violations.append("error_both_resolved_and_unresolved")
        if self.unresolved_errors and _looks_like_completion(self.proposed_next_action):
            violations.append("unresolved_errors_with_completion_action")
        if self.files_touched and self.body:
            mentioned = any(
                path in self.body or path.rsplit("/", 1)[-1] in self.body
                for path in self.files_touched
            )
            if not mentioned:
                violations.append("touched_files_not_summarized")
        return SummaryAudit(tuple(violations))


@dataclass(frozen=True)
class CompactionDecision:
    """Whether and why the policy wants to compact at this step."""

    should_compact: bool
    trigger: CompactTrigger
    reason: str
    checkpoint_step: int
    cooldown_remaining: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "should_compact": self.should_compact,
            "trigger": self.trigger.value,
            "reason": self.reason,
            "checkpoint_step": self.checkpoint_step,
            "cooldown_remaining": self.cooldown_remaining,
        }


@dataclass(frozen=True)
class TrajectorySegment:
    """A monotonic prefix segment created at a compaction boundary."""

    index: int
    start_step: int
    end_step: int
    advantage_share: float

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "start_step": self.start_step,
            "end_step": self.end_step,
            "advantage_share": round(self.advantage_share, 6),
        }


def build_segments(
    total_steps: int, checkpoints: tuple[int, ...] = ()
) -> tuple[TrajectorySegment, ...]:
    """Split ``[0, total_steps]`` at compaction checkpoints.

    Every segment shares the same terminal outcome, so the returned plan assigns
    an equal advantage share to each non-empty segment.
    """

    total = max(0, int(total_steps))
    boundaries = sorted({0, total, *(max(0, min(total, int(value))) for value in checkpoints)})
    pairs: list[tuple[int, int]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end > start:
            pairs.append((start, end))
    if not pairs:
        return ()
    share = 1.0 / len(pairs)
    return tuple(
        TrajectorySegment(index=index + 1, start_step=start, end_step=end, advantage_share=share)
        for index, (start, end) in enumerate(pairs)
    )


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of one compaction attempt."""

    decision: CompactionDecision
    audit: SummaryAudit
    summary: str
    checkpoint_step: int
    segments: tuple[TrajectorySegment, ...]
    accepted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.to_dict(),
            "audit": self.audit.to_dict(),
            "summary": self.summary,
            "checkpoint_step": self.checkpoint_step,
            "segments": [segment.to_dict() for segment in self.segments],
            "accepted": self.accepted,
        }


class AutoCompactPolicy:
    """Deterministic policy for agent-initiated context compaction."""

    def __init__(
        self,
        *,
        budget_tokens: int = 2000,
        soft_ratio: float = 0.7,
        hard_ratio: float = 0.9,
        min_steps_between_compactions: int = 3,
    ) -> None:
        if not 0.0 < soft_ratio <= hard_ratio <= 1.0:
            raise ValueError("ratios must satisfy 0 < soft <= hard <= 1")
        if min_steps_between_compactions < 1:
            raise ValueError("min_steps_between_compactions must be >= 1")
        self.budget_tokens = max(1, int(budget_tokens))
        self.soft_ratio = soft_ratio
        self.hard_ratio = hard_ratio
        self.min_steps_between_compactions = min_steps_between_compactions
        self._last_checkpoint: int | None = None
        self._checkpoints: list[int] = []

    @property
    def checkpoints(self) -> tuple[int, ...]:
        return tuple(self._checkpoints)

    def _cooldown(self, step: int) -> int:
        if self._last_checkpoint is None:
            return 0
        elapsed = step - self._last_checkpoint
        return max(0, self.min_steps_between_compactions - elapsed)

    def decide(
        self,
        state: WorkingState,
        *,
        trigger: CompactTrigger | None = None,
    ) -> CompactionDecision:
        budget_tokens = state.budget_tokens or self.budget_tokens
        ratio = state.used_tokens / budget_tokens
        if trigger is None:
            if state.phase in state.completed_phases:
                trigger = CompactTrigger.PHASE_COMPLETE
            elif ratio >= self.hard_ratio:
                trigger = CompactTrigger.BUDGET_PRESSURE
            elif state.has_unresolved_errors and state.phase == "repair":
                trigger = CompactTrigger.ERROR_RECOVERY
            elif ratio >= self.soft_ratio:
                trigger = CompactTrigger.BUDGET_PRESSURE
            else:
                trigger = CompactTrigger.MANUAL

        cooldown = self._cooldown(state.step)
        if cooldown > 0 and trigger is not CompactTrigger.MANUAL:
            return CompactionDecision(
                should_compact=False,
                trigger=trigger,
                reason="compaction cooldown is still active",
                checkpoint_step=state.step,
                cooldown_remaining=cooldown,
            )

        should_compact = True
        reason = f"trigger {trigger.value}"
        if trigger is CompactTrigger.BUDGET_PRESSURE and ratio < self.soft_ratio:
            should_compact = False
            reason = "budget pressure trigger below the soft threshold"
        elif trigger is CompactTrigger.PHASE_COMPLETE and state.phase not in state.completed_phases:
            should_compact = False
            reason = "phase-complete trigger without a completed phase"
        elif trigger is CompactTrigger.ERROR_RECOVERY and not state.has_unresolved_errors:
            should_compact = False
            reason = "error-recovery trigger without unresolved errors"
        elif trigger is CompactTrigger.MANUAL:
            reason = "manual compaction requested"

        return CompactionDecision(
            should_compact=should_compact,
            trigger=trigger,
            reason=reason,
            checkpoint_step=state.step,
            cooldown_remaining=cooldown,
        )

    @staticmethod
    def build_summary(state: WorkingState) -> SummaryDraft:
        """Produce the deterministic working-state summary for a checkpoint."""

        files = ", ".join(state.files_touched) or "none"
        errors = "; ".join(state.unresolved_errors) or "none"
        action = state.proposed_next_action or "continue the current phase"
        body = "\n".join(
            (
                "# Auto Context Summary",
                f"- Phase: {state.phase or 'unknown'}",
                f"- Step: {state.step}",
                f"- Files touched: {files}",
                f"- Unresolved errors: {errors}",
                f"- Proposed next action: {action}",
            )
        )
        return SummaryDraft(
            body=body,
            unresolved_errors=state.unresolved_errors,
            resolved_errors=state.resolved_errors,
            proposed_next_action=state.proposed_next_action,
            files_touched=state.files_touched,
        )

    def compact(
        self,
        state: WorkingState,
        summary: SummaryDraft | None = None,
        *,
        trigger: CompactTrigger | None = None,
    ) -> CompactionResult:
        """Evaluate a summary and, when consistent, accept the checkpoint."""

        decision = self.decide(state, trigger=trigger)
        draft = summary or self.build_summary(state)
        audit = draft.audit()
        accepted = decision.should_compact and audit.consistent
        if accepted:
            self._last_checkpoint = state.step
            self._checkpoints.append(state.step)
            segments = build_segments(state.step, tuple(self._checkpoints))
        else:
            segments = build_segments(state.step, tuple(self._checkpoints))
        return CompactionResult(
            decision=decision,
            audit=audit,
            summary=draft.body if accepted else "",
            checkpoint_step=state.step,
            segments=segments,
            accepted=accepted,
        )

    def reset(self) -> None:
        self._last_checkpoint = None
        self._checkpoints.clear()
