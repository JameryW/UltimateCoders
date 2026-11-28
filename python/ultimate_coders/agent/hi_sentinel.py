"""HiSentinel: pre-execution Allow / Redirect / Hard-Pause triage (#32).

Ledger item #32 adapts HiSentinel (arXiv:2609.39957).  Coding agents often make
the decisive mistake around step 7, lock in an irreversible failure around step
12, and only surface an explicit error around step 16.  A lightweight sentinel
sits between action proposal and execution and returns one of three decisions:

* ``Allow`` -- the action is safe or the agent is already repairing a local
  blemish;
* ``Redirect`` -- block the action and inject a short evidence-based correction;
* ``Hard-pause`` -- a human-only requirement or authorisation is missing.

The module also records the hindsight-gated intervention distillation (HGID)
signal and requires a full causal prefix on monitored steps.

Feature flag: ``UC_HI_SENTINEL`` (default off).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "UC_HI_SENTINEL",
    "SentinelDecision",
    "ActionRisk",
    "ActionProposal",
    "SentinelVerdict",
    "HgidGate",
    "HiSentinel",
    "hi_sentinel_enabled",
    "hgid_helpfulness_gate",
]

UC_HI_SENTINEL = "UC_HI_SENTINEL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_RISKY_ACTIONS = frozenset({"edit_file", "write_file", "submit_patch", "apply_patch", "shell"})
_DESTRUCTIVE_ACTIONS = frozenset({"delete_file", "git_reset", "git_checkout", "shell"})


def hi_sentinel_enabled() -> bool:
    """Return whether the pre-execution sentinel is enabled."""

    return os.environ.get(UC_HI_SENTINEL, "").strip().lower() in _TRUTHY


class SentinelDecision(str, Enum):
    ALLOW = "allow"
    REDIRECT = "redirect"
    HARD_PAUSE = "hard_pause"


class ActionRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ActionProposal:
    """Minimal description of an action awaiting execution."""

    action: str
    target: str = ""
    summary: str = ""
    step: int = 0
    touches_protected: bool = False
    has_evidence: bool = False
    requires_human_preference: bool = False
    authorization_present: bool = True
    confidence: float = 0.8
    destructive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", str(self.action).strip())
        object.__setattr__(self, "target", str(self.target).strip())
        object.__setattr__(self, "summary", str(self.summary).strip())
        object.__setattr__(self, "step", max(0, int(self.step)))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))

    @property
    def is_risky(self) -> bool:
        return self.action in _RISKY_ACTIONS or self.destructive

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "target": self.target,
            "summary": self.summary,
            "step": self.step,
            "touches_protected": self.touches_protected,
            "has_evidence": self.has_evidence,
            "requires_human_preference": self.requires_human_preference,
            "authorization_present": self.authorization_present,
            "confidence": round(self.confidence, 6),
            "destructive": self.destructive,
            "risk": self.risk.value,
        }

    @property
    def risk(self) -> ActionRisk:
        if self.destructive or self.touches_protected:
            return ActionRisk.HIGH
        if not self.has_evidence or self.confidence < 0.55:
            return ActionRisk.MEDIUM
        return ActionRisk.LOW


@dataclass(frozen=True)
class SentinelVerdict:
    decision: SentinelDecision
    risk: ActionRisk
    reason: str
    redirect_instruction: str = ""
    pause_question: str = ""
    full_prefix_recompute: bool = True

    @property
    def blocked(self) -> bool:
        return self.decision is not SentinelDecision.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "risk": self.risk.value,
            "reason": self.reason,
            "redirect_instruction": self.redirect_instruction,
            "pause_question": self.pause_question,
            "full_prefix_recompute": self.full_prefix_recompute,
        }


@dataclass(frozen=True)
class HgidGate:
    """Whether future information actually improved the teacher's decision."""

    helpful: bool
    teacher_label: str
    confidence_with_future: float
    confidence_without_future: float

    def to_dict(self) -> dict[str, object]:
        return {
            "helpful": self.helpful,
            "teacher_label": self.teacher_label,
            "confidence_with_future": round(self.confidence_with_future, 6),
            "confidence_without_future": round(self.confidence_without_future, 6),
        }


def _argmax(scores: Mapping[str, float]) -> tuple[str, float]:
    if not scores:
        return "", 0.0
    label = max(scores, key=lambda key: (float(scores[key]), key))
    return label, float(scores[label])


def hgid_helpfulness_gate(
    with_future: Mapping[str, float],
    without_future: Mapping[str, float],
    label: str,
) -> HgidGate:
    """Apply the HiSentinel helpfulness gate for privileged distillation.

    The teacher sees the future outcome in ``with_future``.  Distillation is
    allowed only when the future did not change the teacher's argmax and the
    teacher became more confident in the correct label.
    """

    best_label, confidence = _argmax(with_future)
    baseline = float(without_future.get(best_label, 0.0))
    helpful = bool(best_label) and best_label == label and confidence > baseline
    return HgidGate(
        helpful=helpful,
        teacher_label=best_label,
        confidence_with_future=confidence,
        confidence_without_future=baseline,
    )


class HiSentinel:
    """Deterministic pre-execution triage policy."""

    def __init__(self, *, redirect_threshold: float = 0.55) -> None:
        if not 0.0 <= redirect_threshold <= 1.0:
            raise ValueError("redirect_threshold must be in [0, 1]")
        self.redirect_threshold = redirect_threshold

    def review(self, proposal: ActionProposal) -> SentinelVerdict:
        """Return the three-way decision for one proposed action."""

        if proposal.requires_human_preference and not proposal.authorization_present:
            return SentinelVerdict(
                decision=SentinelDecision.HARD_PAUSE,
                risk=proposal.risk,
                reason="a human-only preference or authorisation is missing",
                pause_question=(
                    "This action needs a preference only you can supply. "
                    "Should the agent proceed, and under which constraint?"
                ),
            )
        if proposal.destructive and proposal.touches_protected:
            return SentinelVerdict(
                decision=SentinelDecision.HARD_PAUSE,
                risk=proposal.risk,
                reason="a destructive action targets a protected test or configuration path",
                pause_question="Confirm deleting or resetting a protected path before execution.",
            )
        if proposal.touches_protected:
            return SentinelVerdict(
                decision=SentinelDecision.REDIRECT,
                risk=proposal.risk,
                reason="the target is protected from agent edits",
                redirect_instruction=(
                    "Do not modify the protected path. Read the interface and implement the "
                    "change in an editable file under the boundary contract."
                ),
            )
        if proposal.action in _DESTRUCTIVE_ACTIONS and not proposal.has_evidence:
            return SentinelVerdict(
                decision=SentinelDecision.REDIRECT,
                risk=proposal.risk,
                reason="a destructive action has no supporting evidence",
                redirect_instruction=(
                    "Collect the failing test, stack trace, or call-site evidence before "
                    "running a destructive command."
                ),
            )
        if proposal.is_risky and not proposal.has_evidence:
            return SentinelVerdict(
                decision=SentinelDecision.REDIRECT,
                risk=proposal.risk,
                reason="an edit or submit action has no evidence attached",
                redirect_instruction=(
                    "Collect the failing test, stack trace, or call-site evidence before "
                    "changing code; read the target symbol and its callers first."
                ),
            )
        if proposal.confidence < self.redirect_threshold:
            return SentinelVerdict(
                decision=SentinelDecision.REDIRECT,
                risk=proposal.risk,
                reason="the sentinel confidence is below the redirect threshold",
                redirect_instruction="Re-check the action against the current working state.",
            )
        return SentinelVerdict(
            decision=SentinelDecision.ALLOW,
            risk=proposal.risk,
            reason="the action is supported by evidence and within the boundary",
        )

    @staticmethod
    def prefix_mode(monitor_step: bool) -> str:
        """Monitoring steps must rebuild the full causal prefix, never a truncated KV."""

        return "full_causal_prefix" if monitor_step else "incremental"
