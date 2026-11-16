"""SWE-Doctor runtime diagnosis (v10 #22).

SWE-Doctor's premise is that most failing agent patches are explained by the
first place where runtime state diverges from the intended behaviour, not by
the final assertion message.  This module extracts that evidence from a
pytest failure log produced with ``--tb=short --showlocals``:

* the expected behaviour clauses (caller-supplied and assertion text from the
  traceback),
* the local variable snapshots printed under the failing frame,
* the first frame that carries local state (the first state-divergence
  frame), and
* a short deterministic summary naming the failure site.

It is a pure text parser.  It never imports the test module, never executes a
test, and never evaluates the values it extracts.  All output is bounded so a
large traceback cannot flood an event or prompt, and repeated runs over the
same text produce identical reports.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_MAX_SNAPSHOTS",
    "SWE_DOCTOR_ENV",
    "ExpectedBehaviour",
    "RuntimeDiagnosisReport",
    "VariableSnapshot",
    "diagnose_runtime",
    "swe_doctor_enabled",
]

#: Environment flag that enables SWE-Doctor diagnosis.
SWE_DOCTOR_ENV = "UC_SWE_DOCTOR"
DEFAULT_MAX_SNAPSHOTS = 20
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MAX_VALUE_CHARS = 240
_MAX_CLAUSE_CHARS = 240
_MAX_SUMMARY_CHARS = 600

_SECTION_RE = re.compile(r"^_{3,}\s*(?P<title>.+?)\s*_{3,}$")
_FRAME_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): in (?P<func>.+?)\s*$")
_LOCAL_RE = re.compile(r"^\s{8,}(?P<name>[A-Za-z_]\w*)\s+=\s(?P<value>.*)$")
_ERROR_RE = re.compile(r"^E\s+(?P<body>.*)$")
_EXPECTED_RE = re.compile(r"(?i)\bexpected\b\s*[:=]\s*(?P<clause>.+)$")
_ASSERT_PREFIX_RE = re.compile(r"^assert\s+(?P<expr>.+)$")


def swe_doctor_enabled() -> bool:
    """Return whether SWE-Doctor diagnosis is enabled via ``UC_SWE_DOCTOR``."""

    return os.environ.get(SWE_DOCTOR_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ExpectedBehaviour:
    """One expected behaviour clause and where it came from."""

    clause: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {"clause": self.clause, "source": self.source}


@dataclass(frozen=True)
class VariableSnapshot:
    """One local variable value captured at a failure frame."""

    name: str
    value: str
    frame: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value, "frame": self.frame}


@dataclass(frozen=True)
class RuntimeDiagnosisReport:
    """Deterministic SWE-Doctor report extracted from one pytest log."""

    expected: tuple[ExpectedBehaviour, ...]
    snapshots: tuple[VariableSnapshot, ...]
    divergence_frame: str
    summary: str

    @property
    def expected_clauses(self) -> tuple[str, ...]:
        return tuple(item.clause for item in self.expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected": [item.to_dict() for item in self.expected],
            "snapshots": [item.to_dict() for item in self.snapshots],
            "divergence_frame": self.divergence_frame,
            "summary": self.summary,
        }


@dataclass
class _Frame:
    """Internal parser record for one traceback frame."""

    test: str
    path: str
    line: int
    func: str
    code_lines: list[str]
    error_lines: list[str]
    locals: list[tuple[str, str]]

    @property
    def label(self) -> str:
        return f"{self.path}:{self.line}: in {self.func}"


def diagnose_runtime(
    test_output: str,
    *,
    expected: Sequence[str | ExpectedBehaviour] = (),
    max_snapshots: int = DEFAULT_MAX_SNAPSHOTS,
) -> RuntimeDiagnosisReport:
    """Parse a pytest ``--tb=short --showlocals`` failure log.

    ``expected`` lets the caller state the intended behaviour in its own
    words; those clauses are kept first (source ``"caller"``).  Assertion text
    found in the traceback is appended with the failing source location as its
    source.  ``max_snapshots`` caps the number of local-variable entries
    returned (0 disables snapshots).  Missing or malformed input yields an
    empty, non-throwing report -- diagnosis must never make a failed test run
    worse.
    """

    if int(max_snapshots) < 0:
        raise ValueError("max_snapshots must be >= 0")
    text = str(test_output or "")
    frames = _parse_frames(text)

    expected_items: list[ExpectedBehaviour] = []
    seen: set[str] = set()
    for clause in expected:
        if isinstance(clause, ExpectedBehaviour):
            cleaned = _clip(clause.clause.strip(), _MAX_CLAUSE_CHARS)
            source = clause.source or "caller"
        else:
            cleaned = _clip(str(clause).strip(), _MAX_CLAUSE_CHARS)
            source = "caller"
        if cleaned and cleaned.lower() not in seen:
            expected_items.append(ExpectedBehaviour(clause=cleaned, source=source))
            seen.add(cleaned.lower())

    for frame in frames:
        clause = _frame_expected_clause(frame)
        if clause and clause.lower() not in seen:
            expected_items.append(
                ExpectedBehaviour(clause=clause, source=f"{frame.path}:{frame.line}")
            )
            seen.add(clause.lower())

    snapshots: list[VariableSnapshot] = []
    for frame in frames:
        if len(snapshots) >= int(max_snapshots):
            break
        for name, value in frame.locals:
            if len(snapshots) >= int(max_snapshots):
                break
            snapshots.append(
                VariableSnapshot(
                    name=name,
                    value=_clip(value, _MAX_VALUE_CHARS),
                    frame=frame.label,
                )
            )

    divergence_frame = _first_divergence_frame(frames)
    summary = _build_summary(frames, expected_items, snapshots, divergence_frame)
    return RuntimeDiagnosisReport(
        expected=tuple(expected_items),
        snapshots=tuple(snapshots),
        divergence_frame=divergence_frame,
        summary=summary,
    )


def _parse_frames(text: str) -> list[_Frame]:
    """Split pytest short tracebacks into frames with code/error/locals."""

    frames: list[_Frame] = []
    current_test = ""
    current: _Frame | None = None
    in_error_block = False

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        section = _SECTION_RE.match(raw_line.strip())
        if section is not None:
            current_test = section.group("title").strip()
            current = None
            in_error_block = False
            continue

        frame_match = _FRAME_RE.match(raw_line)
        if frame_match is not None:
            current = _Frame(
                test=current_test,
                path=frame_match.group("path").strip(),
                line=int(frame_match.group("line")),
                func=frame_match.group("func").strip(),
                code_lines=[],
                error_lines=[],
                locals=[],
            )
            frames.append(current)
            in_error_block = False
            continue

        if current is None:
            continue

        error_match = _ERROR_RE.match(raw_line)
        if error_match is not None:
            body = error_match.group("body").strip()
            if body:
                current.error_lines.append(body)
            in_error_block = True
            continue

        if in_error_block:
            local_match = _LOCAL_RE.match(raw_line)
            if local_match is not None:
                current.locals.append(
                    (local_match.group("name"), local_match.group("value").rstrip())
                )
                continue
            if raw_line.strip() == "":
                in_error_block = False
            # ``E    + where ...`` continuations and indented reprs are
            # intentionally ignored; only the first E line carries the clause.
            continue

        stripped = raw_line.strip()
        if stripped and raw_line[:1].isspace() and not stripped.startswith("E"):
            current.code_lines.append(stripped)

    return frames


def _frame_expected_clause(frame: _Frame) -> str:
    """Extract the intended assertion expression from one failure frame."""

    for line in frame.code_lines:
        match = _ASSERT_PREFIX_RE.match(line)
        if match is not None:
            return _clip(match.group("expr").strip(), _MAX_CLAUSE_CHARS)
    for line in frame.error_lines:
        expected_match = _EXPECTED_RE.search(line)
        if expected_match is not None:
            return _clip(expected_match.group("clause").strip(), _MAX_CLAUSE_CHARS)
        match = _ASSERT_PREFIX_RE.match(line)
        if match is not None:
            return _clip(match.group("expr").strip(), _MAX_CLAUSE_CHARS)
    return ""


def _first_divergence_frame(frames: Iterable[_Frame]) -> str:
    first_label = ""
    for frame in frames:
        if not first_label:
            first_label = frame.label
        if frame.locals:
            return frame.label
    return first_label


def _build_summary(
    frames: Sequence[_Frame],
    expected: Sequence[ExpectedBehaviour],
    snapshots: Sequence[VariableSnapshot],
    divergence_frame: str,
) -> str:
    if not frames:
        return "No runtime failure frames found in the test output."
    frame = next((item for item in frames if item.label == divergence_frame), frames[0])
    clause = expected[0].clause if expected else "not stated"
    if snapshots:
        state = ", ".join(f"{item.name}={item.value}" for item in snapshots[:3])
        detail = f"First state divergence at {divergence_frame}; observed {state}."
    else:
        detail = f"First state divergence at {divergence_frame}; no local snapshots captured."
    summary = (
        f"{frame.func} failed at {frame.path}:{frame.line}. "
        f"Expected: {clause}. {detail}"
    )
    return _clip(summary, _MAX_SUMMARY_CHARS)


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
