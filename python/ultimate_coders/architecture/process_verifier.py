"""Deterministic process-level acceptance review for agent-authored changes.

Outcome-based checks ("did the tests pass?") miss changes that make a test
incapable of failing.  This module complements them with a cheap, purely
static line-diff review in the spirit of SWE-Critix:

* ``deleted_assertion`` - a test assertion was removed and not replaced.
* ``test_bypass`` - a skip/xfail marker, an early ``return`` or an added
  product-code branch that disables work under a test/CI environment flag.
* ``exception_swallow`` - a newly added ``except`` block that only
  ``pass``/``continue``s.
* ``lucky_pass`` - test functions were added/removed without a new
  assertion (or an added test body cannot fail).

The heuristics are intentionally simple and documented; there is no LLM and
no code execution.  Every result is deterministic for the same inputs.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_ENFORCE_ENV = "UC_PROCESS_VERIFIER_ENFORCE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

CLEAN_SUMMARY = "No process-level acceptance risk detected."
_MAX_CAUSAL_LINES = 8
_MAX_DETAIL_CHARS = 160

_ASSERTION_MARKERS = ("assert", "self.assert", "expect(", "pytest.raises")
_TEST_BYPASS_MARKERS = (
    "pytest.skip(",
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "@unittest.skip",
    "unittest.SkipTest",
    "pytest.xfail",
    "xfail(",
)
_ENV_GUARD_TOKENS = ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS", "CI", "TESTING")
_EARLY_EXITS = frozenset({"return", "return None", "pass", "sys.exit(0)"})
_SWALLOW_BODY = frozenset({"pass", "continue"})
_SWALLOW_RE = re.compile(
    r"^\s*except\s*(?:Exception|BaseException)?\s*(?:as\s+\w+)?\s*:\s*(?:#.*)?$"
)
_TEST_FUNCTION_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+(test_\w+)\s*\(")
_HELPER_CALL_RE = re.compile(r"\b\w*(?:assert|check|expect|verify|test|helper)\w*\s*\(")


@dataclass(frozen=True)
class ProcessFinding:
    """One process-level acceptance finding."""

    kind: str
    severity: str
    path: str
    line: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProcessReviewReport:
    """Aggregate result of :func:`verify_process`."""

    findings: tuple[ProcessFinding, ...]
    passed: bool
    causal_summary: str

    @property
    def errors(self) -> tuple[ProcessFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "error")

    @property
    def warnings(self) -> tuple[ProcessFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [item.to_dict() for item in self.findings],
            "passed": self.passed,
            "causal_summary": self.causal_summary,
        }


class ProcessReviewRejected(RuntimeError):  # noqa: N818 - name is part of the public API
    """Raised when a strict gate rejects a report with errors."""

    def __init__(self, report: ProcessReviewReport) -> None:
        self.report = report
        super().__init__(report.causal_summary or "process review rejected")


def process_verifier_enabled() -> bool:
    """Return whether the enforcement gate is enabled via ``UC_PROCESS_VERIFIER_ENFORCE``."""

    return _truthy_env(_ENFORCE_ENV)


def apply_process_review(report: ProcessReviewReport, *, strict: bool = True) -> None:
    """Raise :class:`ProcessReviewRejected` when enforcement is on and errors exist.

    ``strict=False`` keeps the gate advisory: it never raises.
    """

    if not process_verifier_enabled() or report.passed or not strict:
        return
    raise ProcessReviewRejected(report)


def verify_process(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    test_paths: Iterable[str] = (),
    max_findings: int = 20,
) -> ProcessReviewReport:
    """Review the line-level delta between two snapshots of a repository.

    ``test_paths`` is unioned with the automatic rule (name contains
    ``test_``, ends with ``_test.py`` or lives under a ``tests/`` directory)
    so callers can flag unusual test locations without losing the default.
    Findings are ordered errors-first, then by path/line; at most
    ``max_findings`` are returned.  ``passed`` reflects *all* findings even
    when the returned list is truncated.
    """

    if max_findings < 1:
        raise ValueError("max_findings must be >= 1")
    explicit_tests = {_normalise_path(path) for path in test_paths}
    findings: list[ProcessFinding] = []
    for path in sorted(set(before) | set(after)):
        before_text = before.get(path) or ""
        after_text = after.get(path) or ""
        if before_text == after_text:
            continue
        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()
        removed, added = _line_changes(before_lines, after_lines)
        is_test = _is_test_path(path, explicit_tests)
        findings.extend(
            _review_path(
                path,
                is_test=is_test,
                before_lines=before_lines,
                after_lines=after_lines,
                removed=removed,
                added=added,
            )
        )

    ordered = sorted(findings, key=_finding_sort_key)
    kept = tuple(ordered[:max_findings])
    has_errors = any(item.severity == "error" for item in ordered)
    error_summary = [
        f"{item.path}:{item.line}: {item.kind} — {item.detail}"
        for item in kept
        if item.severity == "error"
    ][:_MAX_CAUSAL_LINES]
    causal_summary = "\n".join(error_summary) if error_summary else CLEAN_SUMMARY
    return ProcessReviewReport(findings=kept, passed=not has_errors, causal_summary=causal_summary)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _normalise_path(path: str) -> str:
    normalised = path.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


def _is_test_path(path: str, explicit: set[str]) -> bool:
    normalised = _normalise_path(path)
    if normalised in explicit or path in explicit:
        return True
    name = normalised.rsplit("/", 1)[-1]
    if "test_" in name or name.endswith("_test.py"):
        return True
    return normalised.startswith(("tests/", "test/")) or "/tests/" in normalised


def _line_changes(
    before_lines: list[str], after_lines: list[str]
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return ``(removed, added)`` as ``(1-based line number, text)`` pairs."""

    removed: list[tuple[int, str]] = []
    added: list[tuple[int, str]] = []
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            for offset, text in enumerate(before_lines[before_start:before_end]):
                removed.append((before_start + offset + 1, text))
        if tag in ("replace", "insert"):
            for offset, text in enumerate(after_lines[after_start:after_end]):
                added.append((after_start + offset + 1, text))
    return removed, added


def _review_path(
    path: str,
    *,
    is_test: bool,
    before_lines: list[str],
    after_lines: list[str],
    removed: list[tuple[int, str]],
    added: list[tuple[int, str]],
) -> list[ProcessFinding]:
    findings: list[ProcessFinding] = []
    findings.extend(_deleted_assertion_findings(path, is_test, removed, added))
    findings.extend(
        _test_bypass_findings(path, is_test=is_test, after_lines=after_lines, added=added)
    )
    findings.extend(_exception_swallow_findings(path, after_lines, added))
    findings.extend(_lucky_pass_findings(path, is_test, after_lines, removed, added))
    return findings


def _has_assertion(text: str) -> bool:
    return any(marker in text for marker in _ASSERTION_MARKERS)


def _shorten(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_DETAIL_CHARS:
        return collapsed
    return collapsed[: _MAX_DETAIL_CHARS - 3] + "..."


def _deleted_assertion_findings(
    path: str,
    is_test: bool,
    removed: list[tuple[int, str]],
    added: list[tuple[int, str]],
) -> list[ProcessFinding]:
    if not is_test:
        return []
    removed_assertions = [
        (line, text) for line, text in removed if text.strip() and _has_assertion(text)
    ]
    if not removed_assertions:
        return []
    added_assertions = sum(1 for _, text in added if _has_assertion(text))
    if added_assertions >= len(removed_assertions):
        return []
    return [
        ProcessFinding(
            kind="deleted_assertion",
            severity="error",
            path=path,
            line=line,
            detail=f"removed test assertion without replacement: {_shorten(text)}",
        )
        for line, text in removed_assertions
    ]


def _test_bypass_findings(
    path: str,
    *,
    is_test: bool,
    after_lines: list[str],
    added: list[tuple[int, str]],
) -> list[ProcessFinding]:
    findings: list[ProcessFinding] = []
    if is_test:
        flagged_lines: set[int] = set()
        for line, text in added:
            marker = next((item for item in _TEST_BYPASS_MARKERS if item in text), None)
            if marker is not None:
                findings.append(
                    ProcessFinding(
                        kind="test_bypass",
                        severity="error",
                        path=path,
                        line=line,
                        detail=f"added test bypass marker {marker!r}",
                    )
                )
                flagged_lines.add(line)
        for line, text in added:
            if line in flagged_lines or text.strip() not in ("return", "return None"):
                continue
            if _return_bypasses_assertion(after_lines, line):
                findings.append(
                    ProcessFinding(
                        kind="test_bypass",
                        severity="error",
                        path=path,
                        line=line,
                        detail="added bare return before an assertion",
                    )
                )
        return findings

    for line, text in added:
        guard = next((token for token in _ENV_GUARD_TOKENS if token in text), None)
        if guard is None or not _looks_like_branch(text):
            continue
        exit_statement = _guard_exit(after_lines, line, text)
        if exit_statement is None:
            continue
        findings.append(
            ProcessFinding(
                kind="test_bypass",
                severity="error",
                path=path,
                line=line,
                detail=f"product branch on {guard} exits early with {exit_statement!r}",
            )
        )
    return findings


def _return_bypasses_assertion(after_lines: list[str], line: int) -> bool:
    """True when a bare return at ``line`` is followed by an assertion in scope."""

    for index in range(line, min(len(after_lines), line + 10)):
        text = after_lines[index]
        stripped = text.strip()
        if not stripped:
            continue
        if _TEST_FUNCTION_RE.match(text) or stripped.startswith(("def ", "class ")):
            return False
        if _has_assertion(text):
            return True
    return False


def _looks_like_branch(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(("if ", "elif "))


def _guard_exit(after_lines: list[str], line: int, text: str) -> str | None:
    """Return the early-exit statement guarded by ``text``, if any."""

    stripped = text.strip()
    if ":" in stripped:
        tail = stripped.split(":", 1)[1].split("#", 1)[0].strip()
        if tail in _EARLY_EXITS:
            return tail
    index = line  # zero-based index of the line *after* ``line`` (1-based).
    while index < len(after_lines):
        candidate = after_lines[index].split("#", 1)[0].strip()
        if candidate:
            return candidate if candidate in _EARLY_EXITS else None
        index += 1
    return None


def _exception_swallow_findings(
    path: str,
    after_lines: list[str],
    added: list[tuple[int, str]],
) -> list[ProcessFinding]:
    findings: list[ProcessFinding] = []
    for line, _ in added:
        if line > len(after_lines):
            continue
        text = after_lines[line - 1]
        if not _SWALLOW_RE.match(text):
            continue
        body = _next_non_blank(after_lines, line)
        if body is None:
            continue
        body_index, body_text = body
        if body_text.strip() not in _SWALLOW_BODY:
            continue
        indent = len(text) - len(text.lstrip())
        body_indent = len(after_lines[body_index]) - len(after_lines[body_index].lstrip())
        if body_indent <= indent:
            continue
        findings.append(
            ProcessFinding(
                kind="exception_swallow",
                severity="warning",
                path=path,
                line=line,
                detail=f"added exception handler whose body is only {body_text.strip()!r}",
            )
        )
    return findings


def _next_non_blank(lines: list[str], line: int) -> tuple[int, str] | None:
    index = line
    while index < len(lines):
        if lines[index].strip():
            return index, lines[index]
        index += 1
    return None


def _lucky_pass_findings(
    path: str,
    is_test: bool,
    after_lines: list[str],
    removed: list[tuple[int, str]],
    added: list[tuple[int, str]],
) -> list[ProcessFinding]:
    if not is_test:
        return []
    added_defs = [
        (line, match)
        for line, text in added
        if (match := _TEST_FUNCTION_RE.match(text)) is not None
    ]
    removed_defs = [line for line, text in removed if _TEST_FUNCTION_RE.match(text)]
    added_assertions = sum(1 for _, text in added if _has_assertion(text))
    findings: list[ProcessFinding] = []
    if (added_defs or removed_defs) and added_assertions == 0:
        findings.append(
            ProcessFinding(
                kind="lucky_pass",
                severity="warning",
                path=path,
                line=1,
                detail="test functions changed without adding an assertion line",
            )
        )
    for line, match in added_defs:
        indent = match.group(1)
        body = _function_body(after_lines, line - 1, indent)
        if not _body_has_check(body):
            findings.append(
                ProcessFinding(
                    kind="lucky_pass",
                    severity="warning",
                    path=path,
                    line=line,
                    detail=f"added test {match.group(2)!r} has no assertion or helper call",
                )
            )
    return findings


def _function_body(
    lines: list[str], def_index: int, def_indent: str
) -> list[tuple[int, str]]:
    """Body lines of the function whose ``def`` is at ``def_index`` (0-based)."""

    base_indent = len(def_indent)
    body: list[tuple[int, str]] = []
    for index in range(def_index + 1, len(lines)):
        text = lines[index]
        if not text.strip():
            body.append((index + 1, text))
            continue
        indent = len(text) - len(text.lstrip())
        if indent <= base_indent:
            break
        body.append((index + 1, text))
    return body


def _body_has_check(body: list[tuple[int, str]]) -> bool:
    for _, text in body:
        if _has_assertion(text) or _HELPER_CALL_RE.search(text):
            return True
    return False


def _finding_sort_key(item: ProcessFinding) -> tuple[int, str, int, str, str]:
    return (0 if item.severity == "error" else 1, item.path, item.line, item.kind, item.detail)
