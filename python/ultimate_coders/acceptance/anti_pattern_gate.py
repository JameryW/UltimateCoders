"""Deterministic anti-pattern acceptance gate for agent-authored patches.

Outcome checks answer "did the tests pass?". They do not answer "did the patch
make the tests meaningless?" or "was the final edit ever tested?". This module
adds a cheap, static, LLM-free review of the delta between two full snapshots
of the repository (``before`` and ``after`` maps of repo-relative path to file
content). It never executes code and has no network access.

The heuristics are deliberately simple and documented:

``no_test_after_final_edit``
    The caller sets ``final_edit_after_tests=True`` when its journal shows a
    non-test ``.py`` edit after the last test run. A diff alone cannot observe
    ordering, so this signal is explicit. One finding is emitted per changed
    non-test Python file.

``test_file_tampering``
    A changed test file was deleted; or a changed test file gains a skip/xfail
    marker (``pytest.skip``, ``pytest.mark.skip``, ``pytest.mark.xfail``,
    ``@unittest.skip``, ``unittest.SkipTest``, ``self.skipTest``); or it loses
    an assertion line without an equal number of replacement assertions; or it
    gains a tautology such as ``assert True``.

``authored_test_never_run``
    A test file was added or modified, but no entry in ``run_commands`` looks
    like a test invocation (``pytest`` / ``py.test`` / ``unittest`` / ``tox`` /
    ``nox``). ``--collect-only`` does not count as running the tests.

``internal_symbol_leak``
    An added non-test Python line imports a ``_``-prefixed module or name from
    outside ``__future__``; or accesses ``module._private``; or adds a
    ``__all__`` entry for a private name. Dunder names are ignored and
    ``self``/``cls`` attribute access is not treated as a cross-module leak.

Findings are ordered errors first, then path/line; at most ``max_findings`` are
returned, while ``passed`` reflects the full finding set. The gate is opt-in
through ``UC_ANTI_PATTERN_GATE``.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ANTI_PATTERN_GATE_ENV",
    "CLEAN_SUMMARY",
    "AntiPatternFinding",
    "AntiPatternReport",
    "anti_pattern_gate_enabled",
    "scan_anti_patterns",
]

ANTI_PATTERN_GATE_ENV = "UC_ANTI_PATTERN_GATE"

CLEAN_SUMMARY = "No acceptance anti-pattern detected."

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_MAX_DETAIL_CHARS = 160
_MAX_SUMMARY_LINES = 8

_TEST_BYPASS_MARKERS = (
    "pytest.skip(",
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "@unittest.skip",
    "unittest.SkipTest",
    "self.skipTest(",
    "pytest.xfail",
    "xfail(",
)
_ASSERTION_MARKERS = ("assert", "self.assert", "expect(", "pytest.raises")
_TAUTOLOGY_LINES = frozenset({"assert True", "assert 1", "assert 1 == 1", "assert (True)"})
_TEST_COMMAND_RE = re.compile(r"\b(?:pytest|py\.test|unittest|tox|nox)\b")
_FROM_IMPORT_RE = re.compile(r"^\s*from\s+([.\w]*)\s+import\s+(.+?)\s*$")
_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+)")
_ALL_START_RE = re.compile(r"^\s*__all__\s*=\s*[\[(](.*)")
_PRIVATE_ATTR_RE = re.compile(r"\b([A-Za-z_]\w*)\.(_[A-Za-z]\w*)\b")
_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")
_NON_LEAK_OBJECTS = frozenset({"self", "cls", "super", "m", "mock"})


@dataclass(frozen=True)
class AntiPatternFinding:
    """One deterministic acceptance anti-pattern finding."""

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
class AntiPatternReport:
    """Aggregate result of :func:`scan_anti_patterns`."""

    findings: tuple[AntiPatternFinding, ...]
    passed: bool
    summary: str

    @property
    def errors(self) -> tuple[AntiPatternFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "error")

    @property
    def warnings(self) -> tuple[AntiPatternFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [item.to_dict() for item in self.findings],
            "passed": self.passed,
            "summary": self.summary,
        }


def anti_pattern_gate_enabled() -> bool:
    """Whether ``UC_ANTI_PATTERN_GATE`` enables the acceptance gate."""
    return os.environ.get(ANTI_PATTERN_GATE_ENV, "").strip().lower() in _TRUTHY_VALUES


def scan_anti_patterns(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    run_commands: Sequence[str] = (),
    final_edit_after_tests: bool = False,
    max_findings: int = 20,
) -> AntiPatternReport:
    """Review the static delta between two repository snapshots.

    ``before`` and ``after`` are full file snapshots, not patches: any path whose
    content differs (including additions and deletions) is reviewed. The
    function is deterministic for the same inputs and never runs a command.
    """
    if max_findings < 1:
        raise ValueError("max_findings must be >= 1")
    commands = [str(command) for command in run_commands or ()]
    test_command_seen = any(_is_test_command(command) for command in commands)

    changed_paths = [
        path
        for path in sorted(set(before) | set(after))
        if _snapshot_text(before, path) != _snapshot_text(after, path)
    ]
    findings: list[AntiPatternFinding] = []
    for path in changed_paths:
        before_text = _snapshot_text(before, path)
        after_text = _snapshot_text(after, path)
        removed, added = _line_changes(before_text.splitlines(), after_text.splitlines())
        is_test = _is_test_path(path)
        is_python = path.lower().endswith(".py")

        if final_edit_after_tests and is_python and not is_test:
            findings.append(
                AntiPatternFinding(
                    kind="no_test_after_final_edit",
                    severity="error",
                    path=path,
                    line=0,
                    detail="non-test Python file changed after the last test run",
                )
            )
        if is_test:
            findings.extend(_test_tampering_findings(path, before, after, removed, added))
            if path in after and not test_command_seen:
                findings.append(
                    AntiPatternFinding(
                        kind="authored_test_never_run",
                        severity="error",
                        path=path,
                        line=0,
                        detail="test file added or modified but no test command was run",
                    )
                )
        elif is_python:
            findings.extend(_internal_symbol_leak_findings(path, added))

    ordered = sorted(findings, key=_finding_sort_key)
    deduped = list(dict.fromkeys(ordered))
    kept = tuple(deduped[:max_findings])
    has_errors = any(item.severity == "error" for item in deduped)
    summary_lines = [
        f"{item.path or '<patch>'}:{item.line}: {item.kind} — {item.detail}"
        for item in kept
        if item.severity == "error"
    ][:_MAX_SUMMARY_LINES]
    summary = "\n".join(summary_lines) if summary_lines else CLEAN_SUMMARY
    return AntiPatternReport(findings=kept, passed=not has_errors, summary=summary)


def _snapshot_text(snapshot: Mapping[str, str], path: str) -> str:
    value = snapshot.get(path, "")
    return value if isinstance(value, str) else str(value)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY_VALUES


def _normalise_path(path: str) -> str:
    normalised = str(path).replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


def _is_test_path(path: str) -> bool:
    normalised = _normalise_path(path)
    name = normalised.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return (
        normalised.startswith(("tests/", "test/"))
        or "/tests/" in normalised
        or "/test/" in normalised
    )


def _is_test_command(command: str) -> bool:
    lowered = command.strip().lower()
    if "--collect-only" in lowered:
        return False
    return bool(_TEST_COMMAND_RE.search(lowered))


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


def _has_assertion(text: str) -> bool:
    return any(marker in text for marker in _ASSERTION_MARKERS)


def _shorten(text: str) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= _MAX_DETAIL_CHARS:
        return collapsed
    return collapsed[: _MAX_DETAIL_CHARS - 3] + "..."


def _test_tampering_findings(
    path: str,
    before: Mapping[str, str],
    after: Mapping[str, str],
    removed: Sequence[tuple[int, str]],
    added: Sequence[tuple[int, str]],
) -> list[AntiPatternFinding]:
    findings: list[AntiPatternFinding] = []
    if path in before and path not in after:
        findings.append(
            AntiPatternFinding(
                kind="test_file_tampering",
                severity="error",
                path=path,
                line=0,
                detail="test file deleted",
            )
        )
        return findings
    for line, text in added:
        marker = next((item for item in _TEST_BYPASS_MARKERS if item in text), None)
        if marker is not None:
            findings.append(
                AntiPatternFinding(
                    kind="test_file_tampering",
                    severity="error",
                    path=path,
                    line=line,
                    detail=f"added test bypass marker {marker!r}",
                )
            )
        stripped = text.strip()
        if stripped in _TAUTOLOGY_LINES:
            findings.append(
                AntiPatternFinding(
                    kind="test_file_tampering",
                    severity="error",
                    path=path,
                    line=line,
                    detail=f"added assertion that cannot fail: {_shorten(stripped)}",
                )
            )
    removed_assertions = [(line, text) for line, text in removed if _has_assertion(text)]
    added_assertions = sum(1 for _, text in added if _has_assertion(text))
    if len(removed_assertions) > added_assertions:
        for line, text in removed_assertions[added_assertions:]:
            findings.append(
                AntiPatternFinding(
                    kind="test_file_tampering",
                    severity="error",
                    path=path,
                    line=line,
                    detail=f"removed test assertion without replacement: {_shorten(text)}",
                )
            )
    return findings


def _internal_symbol_leak_findings(
    path: str, added: Sequence[tuple[int, str]]
) -> list[AntiPatternFinding]:
    findings: list[AntiPatternFinding] = []
    for line, text in added:
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _FROM_IMPORT_RE.match(text)
        if match is not None:
            module = match.group(1)
            if _module_is_private(module):
                findings.append(_leak(path, line, f"imports private module {module!r}"))
            for name in _imported_names(match.group(2)):
                if _name_is_private(name):
                    findings.append(_leak(path, line, f"imports private symbol {name!r}"))
            continue
        match = _IMPORT_RE.match(text)
        if match is not None:
            module = match.group(1)
            if _module_is_private(module):
                findings.append(_leak(path, line, f"imports private module {module!r}"))
            continue
        match = _ALL_START_RE.match(text)
        if match is not None:
            for quoted in _QUOTED_RE.findall(match.group(1)):
                if _name_is_private(quoted):
                    findings.append(
                        _leak(path, line, f"exports private symbol {quoted!r} in __all__")
                    )
            continue
        for object_name, attribute in _PRIVATE_ATTR_RE.findall(text):
            if object_name in _NON_LEAK_OBJECTS or _is_dunder(attribute):
                continue
            findings.append(
                _leak(path, line, f"accesses private attribute {object_name}.{attribute}")
            )
    return findings


def _leak(path: str, line: int, detail: str) -> AntiPatternFinding:
    return AntiPatternFinding(
        kind="internal_symbol_leak",
        severity="error",
        path=path,
        line=line,
        detail=detail,
    )


def _module_is_private(module: str) -> bool:
    parts = [part for part in str(module).split(".") if part]
    if not parts or parts[0] == "__future__":
        return False
    return any(part.startswith("_") and not _is_dunder(part) for part in parts)


def _name_is_private(name: str) -> bool:
    name = str(name).strip()
    if not name or name == "*":
        return False
    return name.startswith("_") and not _is_dunder(name)


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _imported_names(body: str) -> list[str]:
    cleaned = body.split("#", 1)[0].strip().strip("()")
    names: list[str] = []
    for part in cleaned.split(","):
        name = part.strip().split(" as ", 1)[0].strip()
        if name:
            names.append(name)
    return names


def _finding_sort_key(item: AntiPatternFinding) -> tuple[int, str, int, str, str]:
    return (0 if item.severity == "error" else 1, item.path, item.line, item.kind, item.detail)
