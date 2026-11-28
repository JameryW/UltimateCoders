"""MEMENTO active-window compaction (ledger item #117).

MEMENTO (arXiv:2611.00244) trains a context manager on synthetic trajectories
whose notes are written after the fact, then calibrates a confidence signal
with RL so inference can fold context into notes and backtrack to an earlier
note when the current one is unreliable.  The active window stays bounded
(for example 20K tokens) even when the underlying task spans millions of
tokens.

This module is a deterministic active-window compactor and note stack.  It
does not train, call a model, or persist anything.

Environment:
    UC_MEMENTO_COMPACTOR=1 to advertise the feature.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

__all__ = [
    "DEFAULT_ACTIVE_WINDOW",
    "MEMENTO_COMPACTOR_ENV",
    "CompactedNote",
    "CompactionResult",
    "MementoCompactor",
    "MementoEntry",
    "MementoError",
    "TracebackResult",
    "memento_compactor_enabled",
]

MEMENTO_COMPACTOR_ENV = "UC_MEMENTO_COMPACTOR"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
DEFAULT_ACTIVE_WINDOW = 20_000
DEFAULT_COMPACT_FRACTION = 0.5
DEFAULT_MAX_NOTES = 64


def memento_compactor_enabled() -> bool:
    """Return whether the opt-in flag is set."""

    return os.environ.get(MEMENTO_COMPACTOR_ENV, "").strip().lower() in _TRUTHY


class MementoError(ValueError):
    """Raised when an entry or compactor configuration is invalid."""


def _validate_entry(step: int, content: str, tokens: int, confidence: float) -> None:
    if step < 0:
        raise MementoError("step must be non-negative")
    if not content.strip():
        raise MementoError("content must not be empty")
    if tokens < 1:
        raise MementoError("tokens must be positive")
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise MementoError("confidence must be in [0, 1]")


@dataclass(frozen=True)
class MementoEntry:
    """One active-window observation."""

    step: int
    content: str
    tokens: int
    confidence: float

    def __post_init__(self) -> None:
        _validate_entry(self.step, self.content, self.tokens, self.confidence)

    def to_dict(self) -> dict[str, object]:
        """Serialise the entry."""

        return {
            "step": self.step,
            "content": self.content,
            "tokens": self.tokens,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class CompactedNote:
    """A folded note kept in the bounded active window."""

    note_id: str
    content: str
    steps: tuple[int, ...]
    tokens: int
    confidence: float

    def __post_init__(self) -> None:
        if not self.note_id.strip():
            raise MementoError("note_id must not be empty")
        if not self.content.strip():
            raise MementoError("note content must not be empty")
        if not self.steps:
            raise MementoError("note steps must not be empty")
        if self.tokens < 1:
            raise MementoError("note tokens must be positive")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise MementoError("note confidence must be in [0, 1]")

    def to_dict(self) -> dict[str, object]:
        """Serialise the note."""

        return {
            "note_id": self.note_id,
            "content": self.content,
            "steps": list(self.steps),
            "tokens": self.tokens,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of one compaction attempt."""

    compacted: bool
    note: CompactedNote | None
    folded_steps: tuple[int, ...]
    active_tokens: int
    active_window: int

    def to_dict(self) -> dict[str, object]:
        """Serialise the result."""

        return {
            "compacted": self.compacted,
            "note": self.note.to_dict() if self.note is not None else None,
            "folded_steps": list(self.folded_steps),
            "active_tokens": self.active_tokens,
            "active_window": self.active_window,
        }


@dataclass(frozen=True)
class TracebackResult:
    """Backtrack target when confidence is too low."""

    target_step: int
    note: CompactedNote
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Serialise the traceback."""

        return {
            "target_step": self.target_step,
            "note": self.note.to_dict(),
            "reason": self.reason,
        }


class MementoCompactor:
    """Strict active-window compactor with an earlier-note backtrack stack."""

    def __init__(
        self,
        *,
        max_window: int = DEFAULT_ACTIVE_WINDOW,
        compact_fraction: float = DEFAULT_COMPACT_FRACTION,
        max_notes: int = DEFAULT_MAX_NOTES,
        confidence_threshold: float = 0.6,
    ) -> None:
        if max_window < 16:
            raise MementoError("max_window must be at least 16 tokens")
        if not math.isfinite(compact_fraction) or not 0.0 < compact_fraction < 1.0:
            raise MementoError("compact_fraction must be in (0, 1)")
        if max_notes < 1:
            raise MementoError("max_notes must be positive")
        if not math.isfinite(confidence_threshold) or not 0.0 <= confidence_threshold <= 1.0:
            raise MementoError("confidence_threshold must be in [0, 1]")
        self.max_window = max_window
        self.compact_fraction = compact_fraction
        self.max_notes = max_notes
        self.confidence_threshold = confidence_threshold
        self._active: list[MementoEntry] = []
        self._notes: list[CompactedNote] = []
        self._note_seq = 0

    @property
    def active(self) -> tuple[MementoEntry, ...]:
        """Active entries in arrival order."""

        return tuple(self._active)

    @property
    def notes(self) -> tuple[CompactedNote, ...]:
        """Notes in creation order."""

        return tuple(self._notes)

    @property
    def note_tokens(self) -> int:
        """Tokens occupied by folded notes."""

        return sum(note.tokens for note in self._notes)

    @property
    def active_tokens(self) -> int:
        """Tokens occupied by active entries and folded notes."""

        return sum(entry.tokens for entry in self._active) + self.note_tokens

    def record(
        self, step: int, content: str, tokens: int, *, confidence: float = 1.0
    ) -> MementoEntry:
        """Append one observation to the active window."""

        entry = MementoEntry(step, content, tokens, confidence)
        self._active.append(entry)
        return entry

    def needs_compaction(self) -> bool:
        """Whether the active window exceeds its budget."""

        return self.active_tokens > self.max_window

    def compact(self) -> CompactionResult:
        """Fold oldest entries into one note until under the target budget."""

        if not self.needs_compaction() or not self._active:
            return CompactionResult(
                compacted=False,
                note=None,
                folded_steps=(),
                active_tokens=self.active_tokens,
                active_window=self.max_window,
            )
        if len(self._notes) >= self.max_notes:
            raise MementoError("note stack is full")
        target = max(1, int(self.max_window * self.compact_fraction))
        folded: list[MementoEntry] = []
        while self._active:
            remaining = sum(entry.tokens for entry in self._active[1:])
            candidate = self._active[0]
            estimated_note = max(
                1, (sum(entry.tokens for entry in folded) + candidate.tokens) // 4
            )
            folded.append(self._active.pop(0))
            if remaining + self.note_tokens + estimated_note <= target:
                break
        if not folded:
            folded.append(self._active.pop(0))
        note_tokens = max(1, sum(entry.tokens for entry in folded) // 4)
        content = " | ".join(
            f"[{entry.step}] {entry.content.strip()}" for entry in folded
        )
        confidence = min(entry.confidence for entry in folded)
        self._note_seq += 1
        note = CompactedNote(
            note_id=f"note-{self._note_seq}",
            content=content,
            steps=tuple(entry.step for entry in folded),
            tokens=note_tokens,
            confidence=confidence,
        )
        self._notes.append(note)
        return CompactionResult(
            compacted=True,
            note=note,
            folded_steps=note.steps,
            active_tokens=self.active_tokens,
            active_window=self.max_window,
        )

    def compact_if_needed(self) -> CompactionResult:
        """Compact only when the active window is over budget."""

        return self.compact()

    def traceback(
        self, target_step: int, *, threshold: float | None = None
    ) -> TracebackResult | None:
        """Return an earlier note when the latest note is below confidence."""

        if target_step < 0:
            raise MementoError("target_step must be non-negative")
        limit = self.confidence_threshold if threshold is None else threshold
        if not math.isfinite(limit) or not 0.0 <= limit <= 1.0:
            raise MementoError("threshold must be in [0, 1]")
        if not self._notes:
            return None
        latest = self._notes[-1]
        if latest.confidence >= limit:
            return None
        for note in reversed(self._notes[:-1]):
            if note.confidence >= limit and max(note.steps) < target_step:
                return TracebackResult(
                    target_step=target_step,
                    note=note,
                    reason=(
                        f"latest note {latest.note_id} confidence "
                        f"{latest.confidence:.2f} < {limit:.2f}"
                    ),
                )
        return None

    def render_active(self, *, max_chars: int = 4000) -> str:
        """Render the active window within a character budget."""

        if max_chars < 1:
            raise MementoError("max_chars must be positive")
        parts = [f"[{entry.step}] {entry.content.strip()}" for entry in self._active]
        parts.extend(f"[{note.note_id}] {note.content}" for note in self._notes)
        text = "\n".join(parts)
        return text[-max_chars:]

    def reset(self) -> None:
        """Clear active entries, notes, and the note sequence."""

        self._active.clear()
        self._notes.clear()
        self._note_seq = 0
