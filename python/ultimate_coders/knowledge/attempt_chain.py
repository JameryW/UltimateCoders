"""Immutable attempt patch rounds for the evidence-first knowledge layer.

A *round* is one attempt's observed change set: its outcome, the parsed ``git
diff``, the revisions it started/ended on and a bounded, log-safe summary.
Rounds are append-only. The runtime record key is the attempt identity
``task_id:subtask_id:dispatch_attempt:attempt`` (see
:meth:`ultimate_coders.knowledge.model.AttemptRef.key`), so recording the same
round again is a no-op while recording *different* content under the same
identity raises :class:`AttemptChainConflictError` instead of overwriting
history. Retry recovery, replanning and dedupe can therefore trust the chain
as "what the previous attempt actually did", not as "what it claimed".

Capture is deliberately bounded and JSON-safe: :func:`parse_git_diff` clips the
captured diff text, :class:`AttemptChainLedger.record` re-applies the configured
file/character ceilings, and nothing is stored outside that budget.

Symbol location is AST-first: :func:`locate_symbols` maps the new-file hunk
ranges of a Python file onto the enclosing function/class/method names and their
start/end lines when source text is available, and falls back to plain line
ranges (``kind="line"``) for non-Python files, missing source, or unparseable
source. Symbol location never raises: a knowledge-capture problem must not fail
an attempt.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from ultimate_coders.knowledge.model import AttemptRef, digest, safe_text, utc_now_iso
from ultimate_coders.knowledge.rollout import max_patch_diff_chars, max_patch_files
from ultimate_coders.runtime_state import RuntimeState

__all__ = [
    "AttemptChainConflictError",
    "AttemptChainLedger",
    "AttemptRound",
    "FilePatch",
    "NAMESPACE",
    "SymbolChange",
    "locate_symbols",
    "parse_git_diff",
]

#: Runtime-state namespace that holds one record per attempt round.
NAMESPACE = "attempt_chain"

#: Hard ceiling on the symbols kept per file when a round is stored.
MAX_ROUND_SYMBOLS = 32

_OUTCOME_LIMIT = 64
_SUMMARY_LIMIT = 2000
_FAILURE_LIMIT = 4000
_VERDICT_LIMIT = 200
_REVISION_LIMIT = 200
_SYMBOL_LIMIT = 200
_PATH_LIMIT = 512
_SCAN_PAGE = 500
_MAX_SCAN = 5000

_FAILURE_OUTCOMES = frozenset(
    {
        "aborted",
        "cancelled",
        "canceled",
        "crashed",
        "error",
        "errored",
        "fail",
        "failed",
        "failure",
        "rejected",
        "stalled",
        "timed_out",
        "timeout",
    }
)
_SUCCESS_OUTCOMES = frozenset(
    {"complete", "completed", "ok", "pass", "passed", "success", "succeeded"}
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
_GIT_HEADER = "diff --git "


# ---------------------------------------------------------------------------
# Small coercion helpers (records are defensive: bad input is bounded, not fatal)
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _one_line(value: Any) -> str:
    """Collapse arbitrary text into one log-safe line."""

    return " ".join(_text(value).split())


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clamp_limit(value: Any, default: int, *, maximum: int = 1000) -> int:
    limit = _as_int(value, default)
    if limit < 0:
        return 0
    return min(limit, maximum)


# ---------------------------------------------------------------------------
# Patch vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolChange:
    """One changed symbol (or raw hunk range) inside a patched file.

    ``kind`` is ``"function"``/``"class"``/``"method"`` for AST-located symbols
    and ``"line"`` for the fallback. ``symbol`` is empty for line fallbacks;
    ``start_line``/``end_line`` are 1-based inclusive new-file line numbers.
    """

    path: str
    symbol: str = ""
    kind: str = "line"
    start_line: int = 0
    end_line: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", safe_text(self.path, limit=_PATH_LIMIT))
        object.__setattr__(self, "symbol", safe_text(self.symbol, limit=_SYMBOL_LIMIT))
        object.__setattr__(self, "kind", _one_line(self.kind).lower() or "line")
        start = max(0, _as_int(self.start_line))
        object.__setattr__(self, "start_line", start)
        object.__setattr__(self, "end_line", max(start, _as_int(self.end_line, start)))

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "kind": self.kind,
            "start_line": int(self.start_line),
            "end_line": int(self.end_line),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SymbolChange:
        data = _mapping(payload)
        return cls(
            path=_text(data.get("path")),
            symbol=_text(data.get("symbol")),
            kind=_text(data.get("kind")) or "line",
            start_line=_as_int(data.get("start_line")),
            end_line=_as_int(data.get("end_line")),
        )


@dataclass(frozen=True)
class FilePatch:
    """One file in a captured diff.

    ``diff`` is the raw ``git diff`` section (header lines plus hunks) clipped
    to the configured budget; ``truncated`` says the clip happened. ``symbols``
    is the AST-first (or line-level) location view of the hunks.
    """

    path: str
    change_type: str = "modified"
    old_path: str = ""
    additions: int = 0
    deletions: int = 0
    diff: str = ""
    truncated: bool = False
    symbols: tuple[SymbolChange, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", safe_text(self.path, limit=_PATH_LIMIT))
        object.__setattr__(self, "old_path", safe_text(self.old_path, limit=_PATH_LIMIT))
        object.__setattr__(
            self, "change_type", _one_line(self.change_type).lower() or "modified"
        )
        object.__setattr__(self, "additions", max(0, _as_int(self.additions)))
        object.__setattr__(self, "deletions", max(0, _as_int(self.deletions)))
        object.__setattr__(self, "diff", _text(self.diff))
        object.__setattr__(self, "truncated", bool(self.truncated))
        symbols: list[SymbolChange] = []
        for item in self.symbols or ():
            if isinstance(item, SymbolChange):
                symbols.append(item)
            elif isinstance(item, Mapping):
                symbols.append(SymbolChange.from_dict(item))
        object.__setattr__(self, "symbols", tuple(symbols))

    def tokens(self) -> int:
        """Rough token estimate (about four characters per token)."""

        text = " ".join(
            [self.path, self.old_path, self.diff]
            + [symbol.symbol for symbol in self.symbols]
        )
        return (len(text) + 3) // 4

    @property
    def size_tokens(self) -> int:
        """Alias of :meth:`tokens` for callers that prefer a property."""

        return self.tokens()

    def summary(self) -> str:
        """One log-safe line; the diff body is never included."""

        marker = {
            "added": "A",
            "deleted": "D",
            "renamed": "R",
            "copied": "C",
        }.get(self.change_type, "M")
        target = f"{self.old_path}->{self.path}" if self.old_path else self.path
        parts = [f"{marker} {_one_line(target)} +{int(self.additions)} -{int(self.deletions)}"]
        names = [symbol.symbol for symbol in self.symbols if symbol.symbol]
        if names:
            parts.append("symbols=" + ",".join(names))
        if self.truncated:
            parts.append("truncated")
        return safe_text(_one_line(" ".join(parts)), limit=300)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "old_path": self.old_path,
            "additions": int(self.additions),
            "deletions": int(self.deletions),
            "diff": self.diff,
            "truncated": bool(self.truncated),
            "symbols": [symbol.to_dict() for symbol in self.symbols],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FilePatch:
        data = _mapping(payload)
        symbols = tuple(
            item if isinstance(item, SymbolChange) else SymbolChange.from_dict(_mapping(item))
            for item in (data.get("symbols") or ())
            if isinstance(item, (SymbolChange, Mapping))
        )
        return cls(
            path=_text(data.get("path")),
            change_type=_text(data.get("change_type")) or "modified",
            old_path=_text(data.get("old_path")),
            additions=_as_int(data.get("additions")),
            deletions=_as_int(data.get("deletions")),
            diff=_text(data.get("diff")),
            truncated=bool(data.get("truncated")),
            symbols=symbols,
        )


@dataclass(frozen=True)
class AttemptRound:
    """One immutable patch round, keyed by its :class:`AttemptRef`."""

    attempt: AttemptRef
    outcome: str = "unknown"
    patches: tuple[FilePatch, ...] = ()
    base_revision: str = ""
    head_revision: str = ""
    summary: str = ""
    failure: str = ""
    test_verdict: str = ""
    created_at: str = ""
    record_version: int = 0

    def __post_init__(self) -> None:
        attempt = self.attempt
        if isinstance(attempt, Mapping):
            attempt = AttemptRef.from_dict(attempt)
        elif not isinstance(attempt, AttemptRef):
            attempt = AttemptRef()
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(
            self,
            "outcome",
            _one_line(safe_text(self.outcome, limit=_OUTCOME_LIMIT)) or "unknown",
        )
        patches: list[FilePatch] = []
        for item in self.patches or ():
            if isinstance(item, FilePatch):
                patches.append(item)
            elif isinstance(item, Mapping):
                patches.append(FilePatch.from_dict(item))
        object.__setattr__(self, "patches", tuple(patches))
        object.__setattr__(
            self, "base_revision", safe_text(self.base_revision, limit=_REVISION_LIMIT)
        )
        object.__setattr__(
            self, "head_revision", safe_text(self.head_revision, limit=_REVISION_LIMIT)
        )
        object.__setattr__(self, "summary", safe_text(self.summary, limit=_SUMMARY_LIMIT))
        object.__setattr__(self, "failure", safe_text(self.failure, limit=_FAILURE_LIMIT))
        object.__setattr__(
            self, "test_verdict", _one_line(safe_text(self.test_verdict, limit=_VERDICT_LIMIT))
        )
        created_at = _one_line(self.created_at)
        object.__setattr__(
            self, "created_at", created_at if created_at else utc_now_iso()
        )
        object.__setattr__(self, "record_version", max(0, _as_int(self.record_version)))

    def key(self) -> str:
        """Runtime record key / attempt identity of this round."""

        return self.attempt.key()

    @property
    def is_failure(self) -> bool:
        """Whether the attempt is known to have failed.

        Explicit failure outcomes win; an unknown outcome counts as a failure
        only when a failure description was recorded.
        """

        outcome = _one_line(self.outcome).lower()
        if outcome in _FAILURE_OUTCOMES:
            return True
        if outcome in _SUCCESS_OUTCOMES:
            return False
        return bool(self.failure.strip())

    @property
    def file_count(self) -> int:
        """How many files the round touched."""

        return len(self.patches)

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """New-side paths of the round, in patch order and deduplicated."""

        return tuple(dict.fromkeys(patch.path for patch in self.patches if patch.path))

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt.to_dict(),
            "outcome": self.outcome,
            "patches": [patch.to_dict() for patch in self.patches],
            "base_revision": self.base_revision,
            "head_revision": self.head_revision,
            "summary": self.summary,
            "failure": self.failure,
            "test_verdict": self.test_verdict,
            "created_at": self.created_at,
            "record_version": int(self.record_version),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AttemptRound:
        data = _mapping(payload)
        attempt = data.get("attempt")
        patches = tuple(
            item if isinstance(item, FilePatch) else FilePatch.from_dict(_mapping(item))
            for item in (data.get("patches") or ())
            if isinstance(item, (FilePatch, Mapping))
        )
        if not isinstance(attempt, AttemptRef):
            attempt = AttemptRef.from_dict(_mapping(attempt))
        return cls(
            attempt=attempt,
            outcome=_text(data.get("outcome")) or "unknown",
            patches=patches,
            base_revision=_text(data.get("base_revision")),
            head_revision=_text(data.get("head_revision")),
            summary=_text(data.get("summary")),
            failure=_text(data.get("failure")),
            test_verdict=_text(data.get("test_verdict")),
            created_at=_text(data.get("created_at")),
            record_version=_as_int(data.get("record_version")),
        )


# ---------------------------------------------------------------------------
# Unified diff parsing
# ---------------------------------------------------------------------------


def _unquote(value: str) -> str:
    """Decode git's C-style quoted path when it is quoted."""

    text = value.strip()
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body = text[1:-1]
    output = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            escape = body[index + 1]
            simple = {
                "a": 7,
                "b": 8,
                "f": 12,
                "n": 10,
                "r": 13,
                "t": 9,
                "v": 11,
                "\\": 92,
                '"': 34,
            }
            if escape in simple:
                output.append(simple[escape])
                index += 2
                continue
            octal = _OCTAL_ESCAPE.match(body, index)
            if octal is not None:
                output.append(int(octal.group(1), 8) & 0xFF)
                index = octal.end()
                continue
        output.extend(char.encode("utf-8", "replace"))
        index += 1
    return output.decode("utf-8", "replace")


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[2:]
    return path


def _split_quoted_paths(text: str) -> list[str]:
    parts: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != '"':
            index += 1
            continue
        index += 1
        start = index
        while index < len(text):
            if text[index] == "\\" and index + 1 < len(text):
                index += 2
                continue
            if text[index] == '"':
                break
            index += 1
        parts.append('"' + text[start:index] + '"')
        index += 1
    return parts


def _path_from_git_header(line: str) -> tuple[str, str]:
    body = line[len(_GIT_HEADER):].strip()
    if body.startswith('"'):
        parts = _split_quoted_paths(body)
        old = _strip_prefix(_unquote(parts[0])) if parts else ""
        new = _strip_prefix(_unquote(parts[1])) if len(parts) > 1 else ""
        return (old, new or old)
    index = 0
    while True:
        index = body.find(" b/", index)
        if index < 0:
            break
        left, right = body[:index], body[index + 1:]
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            path = left[2:]
            return (path, path)
        index += 1
    index = body.rfind(" b/")
    if index > 0 and body.startswith("a/"):
        return (body[2:index], body[index + 3:])
    if body.startswith("a/"):
        return (body[2:], body[2:])
    return ("", "")


def _header_ref(value: str) -> str:
    text = value.rstrip()
    if "\t" in text:
        text = text.split("\t", 1)[0]
    return _unquote(text.strip())


def _git_path(value: str) -> str:
    return _unquote(value.strip())


def _split_diff_sections(text: str) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith(_GIT_HEADER):
            if current is not None:
                sections.append(current)
            current = [line]
            continue
        if current is None:
            if line.startswith("--- ") or line.startswith("---\t"):
                current = [line]
            continue
        current.append(line)
    if current is not None:
        sections.append(current)
    return sections


def _parse_diff_section(lines: list[str]) -> FilePatch | None:
    if not lines:
        return None
    header_path = ("", "")
    old_ref = ""
    new_ref = ""
    rename_from = ""
    rename_to = ""
    copy_from = ""
    copy_to = ""
    change_type = "modified"
    additions = 0
    deletions = 0
    in_hunk = False
    start_index = 0
    if lines[0].startswith(_GIT_HEADER):
        header_path = _path_from_git_header(lines[0])
        start_index = 1
    for line in lines[start_index:]:
        if line.startswith("@@"):
            if _HUNK_RE.match(line) is not None:
                in_hunk = True
            continue
        if in_hunk:
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
            continue
        if line.startswith("new file mode"):
            change_type = "added"
        elif line.startswith("deleted file mode"):
            change_type = "deleted"
        elif line.startswith("rename from "):
            rename_from = _git_path(line[len("rename from "):])
        elif line.startswith("rename to "):
            rename_to = _git_path(line[len("rename to "):])
        elif line.startswith("copy from "):
            copy_from = _git_path(line[len("copy from "):])
        elif line.startswith("copy to "):
            copy_to = _git_path(line[len("copy to "):])
        elif line.startswith("--- "):
            old_ref = _header_ref(line[4:])
        elif line.startswith("+++ "):
            new_ref = _header_ref(line[4:])
    path = ""
    old_path = ""
    if rename_to or copy_to:
        path = rename_to or copy_to
        old_path = rename_from or copy_from
        change_type = "renamed" if rename_to else "copied"
    if not path and new_ref and new_ref != "/dev/null":
        path = _strip_prefix(new_ref)
    if not path and old_ref and old_ref != "/dev/null":
        path = _strip_prefix(old_ref)
    if not path:
        path = header_path[1] or header_path[0]
    if not path:
        return None
    if change_type == "modified":
        if old_ref == "/dev/null":
            change_type = "added"
        elif new_ref == "/dev/null":
            change_type = "deleted"
    old_side = _strip_prefix(old_ref) if old_ref and old_ref != "/dev/null" else ""
    if not old_path and old_side and old_side != path:
        old_path = old_side
    if old_path == path:
        old_path = ""
    diff_text = "\n".join(lines)
    return FilePatch(
        path=path,
        change_type=change_type,
        old_path=old_path,
        additions=additions,
        deletions=deletions,
        diff=diff_text,
        truncated=False,
        symbols=locate_symbols(path, diff_text),
    )


def _clip_patch_diffs(patches: list[FilePatch], budget: int) -> list[FilePatch]:
    """Clip the captured diff text to one total character budget."""

    remaining = max(0, budget)
    clipped: list[FilePatch] = []
    for patch in patches:
        diff = patch.diff
        truncated = bool(patch.truncated)
        if len(diff) > remaining:
            diff = diff[:remaining]
            truncated = True
        remaining = max(0, remaining - len(diff))
        clipped.append(replace(patch, diff=diff, truncated=truncated))
    return clipped


def parse_git_diff(
    text: Any,
    *,
    max_files: int | None = None,
    max_diff_chars: int | None = None,
) -> tuple[FilePatch, ...]:
    """Parse a unified ``git diff`` into bounded :class:`FilePatch` values.

    New, deleted, modified, renamed and copied files are recognised from the
    ``diff --git`` section headers, ``---``/``+++`` refs and hunk headers.
    ``additions``/``deletions`` count hunk lines. ``max_files`` caps how many
    files are returned and ``max_diff_chars`` is the total character budget for
    the captured diff text of the round (files are clipped in path order and
    marked ``truncated``); ``None`` applies the configured rollout defaults.
    The result is sorted by path, deterministic, and malformed input yields
    whatever could be parsed instead of raising.
    """

    try:
        raw = _text(text)
        file_cap = max(0, max_patch_files() if max_files is None else _as_int(max_files))
        char_cap = max(
            0, max_patch_diff_chars() if max_diff_chars is None else _as_int(max_diff_chars)
        )
        patches: list[FilePatch] = []
        for lines in _split_diff_sections(raw):
            try:
                patch = _parse_diff_section(lines)
            except Exception:
                patch = None
            if patch is not None:
                patches.append(patch)
        patches.sort(key=lambda item: (item.path, item.old_path, item.change_type))
        patches = patches[:file_cap]
        return tuple(_clip_patch_diffs(patches, char_cap))
    except Exception:
        return ()


# ---------------------------------------------------------------------------
# AST-first symbol location
# ---------------------------------------------------------------------------


def _hunk_ranges(diff: Any) -> list[tuple[int, int]]:
    """New-file ``(start, end)`` line ranges of every hunk in ``diff``."""

    ranges: list[tuple[int, int]] = []
    for line in _text(diff).splitlines():
        match = _HUNK_RE.match(line)
        if match is None:
            continue
        new_start = _as_int(match.group(3))
        new_count = _as_int(match.group(4), 1) if match.group(4) is not None else 1
        start = new_start if new_start > 0 else 1
        end = start if new_count <= 0 else start + new_count - 1
        ranges.append((start, end))
    return ranges


def _node_end(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", None)
    if end is None:
        return _as_int(getattr(node, "lineno", 0))
    return _as_int(end)


def _symbol_nodes(source: str) -> list[tuple[str, str, int, int]]:
    """Qualified ``(symbol, kind, start_line, end_line)`` of every def/class."""

    tree = ast.parse(source)
    nodes: list[tuple[str, str, int, int]] = []

    def visit(container: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(container):
            if isinstance(child, ast.ClassDef):
                qualified = f"{prefix}{child.name}"
                nodes.append((qualified, "class", child.lineno, _node_end(child)))
                visit(child, f"{qualified}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}{child.name}"
                kind = "method" if isinstance(container, ast.ClassDef) else "function"
                nodes.append((qualified, kind, child.lineno, _node_end(child)))
                visit(child, f"{qualified}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return nodes


def _innermost_nodes(
    nodes: list[tuple[str, str, int, int]], start: int, end: int
) -> list[tuple[str, str, int, int]]:
    intersecting = [node for node in nodes if node[2] <= end and node[3] >= start]
    if not intersecting:
        return []
    innermost: list[tuple[str, str, int, int]] = []
    for candidate in intersecting:
        candidate_span = candidate[3] - candidate[2]
        contains_smaller = any(
            other is not candidate
            and other[2] >= candidate[2]
            and other[3] <= candidate[3]
            and (other[3] - other[2]) < candidate_span
            for other in intersecting
        )
        if not contains_smaller:
            innermost.append(candidate)
    innermost.sort(key=lambda node: (node[2], node[3], node[0]))
    return innermost


def locate_symbols(
    path: Any,
    diff: Any,
    *,
    source_text: Any = "",
    max_symbols: int = 20,
) -> tuple[SymbolChange, ...]:
    """Locate the symbols (or line ranges) a captured diff touches.

    AST-first: for a ``.py`` path with parsable ``source_text`` the enclosing
    function/class/method names and their start/end lines are returned, with
    nested symbols qualified (``Class.method``). Otherwise every hunk becomes a
    line-level :class:`SymbolChange` with ``kind="line"``. Never raises.
    """

    try:
        raw_path = _text(path)
        ranges = _hunk_ranges(diff)
        limit = _as_int(max_symbols, 20)
        if not ranges or limit <= 0:
            return ()
        nodes: list[tuple[str, str, int, int]] = []
        if raw_path.strip().lower().endswith(".py"):
            source = _text(source_text)
            if source.strip():
                try:
                    nodes = _symbol_nodes(source)
                except (SyntaxError, ValueError, MemoryError, RecursionError):
                    nodes = []
        found: dict[tuple, SymbolChange] = {}
        for start, end in ranges:
            selected = _innermost_nodes(nodes, start, end) if nodes else []
            if selected:
                for qualified, kind, node_start, node_end in selected:
                    key = (raw_path, qualified, kind, node_start, node_end)
                    if key not in found:
                        found[key] = SymbolChange(
                            path=raw_path,
                            symbol=qualified,
                            kind=kind,
                            start_line=node_start,
                            end_line=node_end,
                        )
            else:
                key = (raw_path, "", "line", start, end)
                if key not in found:
                    found[key] = SymbolChange(
                        path=raw_path,
                        symbol="",
                        kind="line",
                        start_line=start,
                        end_line=end,
                    )
        ordered = sorted(
            found.values(), key=lambda item: (item.start_line, item.end_line, item.symbol)
        )
        return tuple(ordered[:limit])
    except Exception:  # pragma: no cover - symbol location must never fail capture
        return ()


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class AttemptChainConflictError(RuntimeError):
    """An attempt identity is already recorded with different content."""


def _coerce_round(value: Any) -> AttemptRound:
    if isinstance(value, AttemptRound):
        return value
    if isinstance(value, Mapping):
        return AttemptRound.from_dict(value)
    raise TypeError("record() expects an AttemptRound or a mapping payload")


def _coerce_ref(value: Any) -> AttemptRef:
    if isinstance(value, AttemptRef):
        return value
    if isinstance(value, Mapping):
        return AttemptRef.from_dict(value)
    raise TypeError("current must be an AttemptRef or a mapping payload")


def _fingerprint(record: AttemptRound) -> str:
    """Digest of the semantic content; audit columns do not participate."""

    payload = record.to_dict()
    payload.pop("created_at", None)
    payload.pop("record_version", None)
    return digest(payload)


def _bound_round(record: AttemptRound) -> AttemptRound:
    """Apply the configured file/character/symbol ceilings to one round."""

    file_cap = max(0, max_patch_files())
    diff_budget = max(0, max_patch_diff_chars())
    patches = list(record.patches)[:file_cap]
    remaining = diff_budget
    bounded: list[FilePatch] = []
    for patch in patches:
        diff = patch.diff
        truncated = bool(patch.truncated)
        if len(diff) > remaining:
            diff = diff[:remaining]
            truncated = True
        remaining = max(0, remaining - len(diff))
        bounded.append(
            replace(
                patch,
                diff=diff,
                truncated=truncated,
                symbols=patch.symbols[:MAX_ROUND_SYMBOLS],
            )
        )
    return replace(record, patches=tuple(bounded))


def _purge(current: dict) -> dict:
    """Tombstone payload for :meth:`AttemptChainLedger.clear`."""

    if isinstance(current, dict) and current.get("cleared"):
        return current
    data = current if isinstance(current, dict) else {}
    return {
        "cleared": True,
        "task_id": _text(data.get("task_id")),
        "subtask_id": _text(data.get("subtask_id")),
    }


class AttemptChainLedger:
    """Append-only store of :class:`AttemptRound` records in :data:`NAMESPACE`.

    Records use the attempt identity as their runtime record key and store the
    task id in the runtime metadata column, so a chain can be scanned per
    task/subtask without a full namespace scan. Writes go through
    :meth:`RuntimeState.mutate`; the record version is read back from the
    runtime store.
    """

    def __init__(self, state: RuntimeState):
        self.state = state

    # -- writes ---------------------------------------------------------

    def record(self, round: Any) -> AttemptRound:
        """Persist one patch round idempotently.

        Replaying identical content returns the stored round untouched (same
        ``created_at``/``record_version``). Recording different content under
        an already-used attempt key raises :class:`AttemptChainConflictError`
        and leaves the original record in place.
        """

        candidate = _bound_round(_coerce_round(round))
        key = candidate.key()
        new_fingerprint = _fingerprint(candidate)
        existing = self.state.versioned_get(NAMESPACE, key)
        if existing is not None:
            stored = self._decode(existing.data, existing.version)
            if stored is not None:
                stored_fingerprint = _fingerprint(stored)
                if stored_fingerprint == new_fingerprint:
                    return stored
                raise self._conflict(key, stored_fingerprint, new_fingerprint)
        payload = candidate.to_dict()
        payload["task_id"] = candidate.attempt.task_id
        payload["subtask_id"] = candidate.attempt.subtask_id
        payload["fingerprint"] = new_fingerprint

        def write(current: dict) -> dict:
            if current and not current.get("cleared"):
                current_fingerprint = _text(current.get("fingerprint"))
                if current_fingerprint == new_fingerprint:
                    return current
                raise self._conflict(key, current_fingerprint, new_fingerprint)
            return payload

        self.state.mutate(NAMESPACE, key, write)
        versioned = self.state.versioned_get(NAMESPACE, key)
        if versioned is None:
            return candidate
        decoded = self._decode(versioned.data, versioned.version)
        return decoded if decoded is not None else candidate

    def clear(self, *, task_id: str, subtask_id: str) -> int:
        """Tombstone one subtask chain; returns how many rounds were cleared."""

        cleared = 0
        for key, data in self._scan(task_id):
            if not self._matches(data, subtask_id):
                continue
            if not isinstance(data.get("attempt"), Mapping):
                continue
            self.state.mutate(NAMESPACE, key, _purge)
            cleared += 1
        return cleared

    # -- reads ----------------------------------------------------------

    def get(self, attempt: AttemptRef | str | Mapping[str, Any]) -> AttemptRound | None:
        """Read one round by :class:`AttemptRef` (or its string key)."""

        key = self._key_of(attempt)
        if not key:
            return None
        versioned = self.state.versioned_get(NAMESPACE, key)
        if versioned is None:
            return None
        return self._decode(versioned.data, versioned.version)

    def rounds(self, *, task_id: str, subtask_id: str, limit: int = 50) -> list[AttemptRound]:
        """Chain of one subtask, oldest first, keeping the newest ``limit``."""

        limit = _clamp_limit(limit, 50)
        if not limit:
            return []
        return self._with_versions(self._history(task_id, subtask_id)[-limit:])

    def prior_rounds(self, current: AttemptRef, *, limit: int = 20) -> list[AttemptRound]:
        """Rounds strictly before ``current.order()``, oldest first."""

        reference = _coerce_ref(current)
        limit = _clamp_limit(limit, 20)
        if not limit:
            return []
        history = self._history(reference.task_id, reference.subtask_id)
        earlier = [record for record in history if record.attempt.order() < reference.order()]
        return self._with_versions(earlier[-limit:])

    def latest(self, *, task_id: str, subtask_id: str) -> AttemptRound | None:
        """Most recent round of the subtask chain, if any."""

        history = self._history(task_id, subtask_id)
        if not history:
            return None
        return self._with_versions(history[-1:])[0]

    # -- internals ------------------------------------------------------

    @staticmethod
    def _conflict(
        key: str, stored_fingerprint: str, new_fingerprint: str
    ) -> AttemptChainConflictError:
        stored = _text(stored_fingerprint)[:16] or "?"
        new = _text(new_fingerprint)[:16]
        return AttemptChainConflictError(
            f"attempt round {key} already recorded with different content "
            f"(stored={stored}, new={new})"
        )

    @staticmethod
    def _key_of(attempt: Any) -> str:
        if isinstance(attempt, AttemptRef):
            return attempt.key()
        if isinstance(attempt, Mapping):
            return AttemptRef.from_dict(attempt).key()
        return _text(attempt).strip()

    @staticmethod
    def _matches(data: Mapping[str, Any], subtask_id: str) -> bool:
        if not subtask_id:
            return True
        return _text(data.get("subtask_id")) == subtask_id

    def _scan(self, task_id: str) -> list[tuple[str, dict]]:
        records: list[tuple[str, dict]] = []
        seen: set[str] = set()
        after = ""
        while True:
            try:
                page = self.state.query(
                    NAMESPACE,
                    task_id=(task_id or None),
                    after=after,
                    limit=_SCAN_PAGE,
                )
            except Exception:  # pragma: no cover - a broken store must not loop forever
                break
            for item in page:
                key = _text(item.get("key"))
                if not key or key in seen:
                    continue
                seen.add(key)
                records.append((key, dict(item)))
            if len(page) < _SCAN_PAGE or len(records) >= _MAX_SCAN:
                break
            after = _text(page[-1].get("key"))
            if not after:
                break
        return records

    def _history(self, task_id: str, subtask_id: str) -> list[AttemptRound]:
        by_key: dict[str, AttemptRound] = {}
        for _key, data in self._scan(task_id):
            if not self._matches(data, subtask_id):
                continue
            record = self._decode(data, None)
            if record is None:
                continue
            if task_id and record.attempt.task_id != task_id:
                continue
            if subtask_id and record.attempt.subtask_id != subtask_id:
                continue
            by_key[record.key()] = record
        return sorted(
            by_key.values(),
            key=lambda item: (
                int(item.attempt.dispatch_attempt),
                int(item.attempt.attempt),
                item.key(),
            ),
        )

    def _with_versions(self, records: list[AttemptRound]) -> list[AttemptRound]:
        stamped: list[AttemptRound] = []
        for record in records:
            versioned = self.state.versioned_get(NAMESPACE, record.key())
            if versioned is None:
                stamped.append(record)
                continue
            stamped.append(replace(record, record_version=max(0, _as_int(versioned.version))))
        return stamped

    @staticmethod
    def _decode(data: Mapping[str, Any], version: int | None) -> AttemptRound | None:
        payload = _mapping(data)
        if not isinstance(payload.get("attempt"), Mapping):
            return None
        try:
            record = AttemptRound.from_dict(payload)
        except (TypeError, ValueError):
            return None
        if version is None:
            return record
        return replace(record, record_version=max(0, _as_int(version)))
