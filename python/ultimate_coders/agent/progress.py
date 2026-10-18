"""Progress detection: is a retry learning anything?

A retry loop that keeps producing the same failure is not progress -- it is
budget burn. The historical gate counted attempts and classified the error;
both are blind to whether attempt N+1 knows anything attempt N did not.

This module compares consecutive attempt evidence along three axes:

* **error signature** -- the normalized failure text (numbers, hex, ANSI and
  whitespace collapsed), so "timeout after 600s" and "timeout after 601s"
  are the same failure;
* **file set** -- which files the attempt changed (a different area touched
  is new information);
* **stderr signature** -- the normalized tail of stderr, which often carries
  the real diagnostic when the summary is generic.

A retry is allowed while at least one axis moves. When none does, the
attempt is *stalled*; after ``stall_limit`` consecutive stalls the verdict
becomes ``stop``: the loop is repeating itself and another identical retry
only burns time. This module never decides *what* to do instead -- there is
no replan primitive in this repository, and inventing one here would be
decoration. It reports ``stop`` with the repeated signature; the caller
(orchestrator, dashboard, operator) decides.

Deterministic on purpose: same evidence in, same verdict out. No LLM, no
clock, no randomness -- the verdict can be asserted in tests and replayed
from an event log.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DIGITS = re.compile(r"\b[0-9a-f]{4,}\b|\d+")
_SPACE = re.compile(r"\s+")


def signature(text: str, *, limit: int = 200) -> str:
    """Normalize a failure string so cosmetic differences collapse."""
    if not text:
        return ""
    cleaned = _ANSI.sub("", str(text)).lower()
    cleaned = _DIGITS.sub("<n>", cleaned)
    cleaned = _SPACE.sub(" ", cleaned).strip()
    return cleaned[:limit]


@dataclass(frozen=True)
class AttemptEvidence:
    """What one attempt observed, in comparable form."""

    attempt: int
    success: bool
    error_signature: str = ""
    stderr_signature: str = ""
    files: tuple[str, ...] = ()

    @classmethod
    def from_result(cls, result, *, attempt: int) -> AttemptEvidence:
        files = tuple(sorted({fc.file_path for fc in (result.modified_files or [])}))
        return cls(
            attempt=attempt,
            success=bool(result.success),
            error_signature=signature(result.error or result.summary or ""),
            stderr_signature=signature(getattr(result, "stderr_tail", "") or ""),
            files=files,
        )


@dataclass(frozen=True)
class ProgressVerdict:
    """Whether another retry is justified, and why."""

    action: str  # "retry" | "stop"
    stalled_streak: int
    reason: str
    signature: str = ""

    @property
    def stalled(self) -> bool:
        return self.stalled_streak > 0


def _evidence_key(evidence: AttemptEvidence) -> str:
    material = "\x1f".join(
        [evidence.error_signature, evidence.stderr_signature, ",".join(evidence.files)]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


@dataclass
class ProgressTracker:
    """Per-subtask tracker; one instance per execute_subtask call."""

    stall_limit: int = 2
    _previous: AttemptEvidence | None = field(default=None, init=False)
    _previous_key: str = field(default="", init=False)
    _streak: int = field(default=0, init=False)

    def observe(self, evidence: AttemptEvidence) -> ProgressVerdict:
        """Fold one attempt into the verdict.

        * first attempt or success -> advancing (nothing to compare yet);
        * any evidence axis moved -> advancing, streak resets;
        * nothing moved -> streak+1; at ``stall_limit`` the verdict is stop.
        """
        if evidence.success:
            self._previous, self._previous_key, self._streak = evidence, "", 0
            return ProgressVerdict("retry", 0, "attempt succeeded")

        key = _evidence_key(evidence)
        if self._previous is None:
            self._previous, self._previous_key = evidence, key
            self._streak = 0
            return ProgressVerdict("retry", 0, "first attempt", evidence.error_signature)

        moved = key != self._previous_key
        self._previous, self._previous_key = evidence, key
        if moved:
            self._streak = 0
            return ProgressVerdict("retry", 0, "new evidence", evidence.error_signature)

        self._streak += 1
        if self._streak >= self.stall_limit:
            return ProgressVerdict(
                "stop",
                self._streak,
                "no new evidence: same error signature, stderr and file set",
                evidence.error_signature,
            )
        return ProgressVerdict("retry", self._streak, "no new evidence", evidence.error_signature)
