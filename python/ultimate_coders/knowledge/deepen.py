"""Bounded git history deepening for the knowledge layer.

A deepening is a *read-only observation* of recent repository history: which
commits are recent, which files they touched and how many lines each file
gained or lost. Pages (``knowledge.pages``) use it as supporting context; it
never mutates the repository and never asserts why a change was made.

Design constraints:

* only read-only git plumbing is executed (``rev-parse``/``log``), always as an
  argument vector with ``shell=False`` and a timeout - never a shell string;
* parsing is split from execution, so tests (and callers holding a fixture log)
  can feed :func:`parse_git_log_numstat` / :func:`deepen_from_log` directly and
  need no repository at all;
* every output is bounded: commit count, distinct tracked paths and total
  changed lines are capped, strings are clipped and integers clamped;
* malformed lines, binary entries and renames are tolerated; nothing raises for
  a missing root, a non-repository or a failing git binary.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import safe_text

__all__ = [
    "DEFAULT_MAX_COMMITS",
    "DEFAULT_MAX_LINES_CHANGED",
    "DEFAULT_MAX_PATHS",
    "GIT_TIMEOUT_SECONDS",
    "MAX_AUTHOR_CHARS",
    "MAX_COMMITS_LIMIT",
    "MAX_LINES_CHANGED_LIMIT",
    "MAX_PARENT_SHAS",
    "MAX_PATHS_LIMIT",
    "MAX_PATH_CHARS",
    "MAX_SHA_CHARS",
    "MAX_SUBJECT_CHARS",
    "CommitInfo",
    "FileChurn",
    "GitDeepen",
    "deepen_for_scope",
    "deepen_from_log",
    "git_deepen",
    "parse_git_log_numstat",
    "parse_git_numstat",
    "parse_git_paths",
    "summarize_deepen",
]

DEFAULT_MAX_COMMITS = 50
MAX_COMMITS_LIMIT = 1_000
DEFAULT_MAX_PATHS = 50
MAX_PATHS_LIMIT = 500
DEFAULT_MAX_LINES_CHANGED = 20_000
MAX_LINES_CHANGED_LIMIT = 1_000_000
MAX_SUBJECT_CHARS = 200
MAX_AUTHOR_CHARS = 120
MAX_PATH_CHARS = 400
MAX_SHA_CHARS = 64
MAX_PARENT_SHAS = 16
GIT_TIMEOUT_SECONDS = 15

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x00"
#: ``git log`` format used by :func:`git_deepen`: NUL-terminated records whose
#: fields are unit-separated, followed by the commit's ``--numstat`` block. The
#: separators are requested through git's own ``%xNN`` escapes because a literal
#: NUL byte cannot be passed in an argv element on Windows.
_LOG_FORMAT = "%H%x1f%P%x1f%an%x1f%aI%x1f%s%x00"
_HEX_DIGITS = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# Small coercion / bounding helpers
# ---------------------------------------------------------------------------


def _text(value: Any, *, limit: int = MAX_PATH_CHARS) -> str:
    """Bounded, stripped text for a stored string field."""

    return safe_text(value, limit=limit).strip()


def _clamp(value: Any, *, default: int, maximum: int) -> int:
    """Clamp a caller-supplied bound into ``[0, maximum]``."""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(maximum, number))


def _count(value: Any) -> int:
    """Clamp a stored counter into ``[0, MAX_LINES_CHANGED_LIMIT]``."""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_LINES_CHANGED_LIMIT, number))


def _sha(value: Any) -> str:
    """Normalize a commit sha (lowercase hex, 4..64 chars) or return ``""``."""

    text = _text(value, limit=MAX_SHA_CHARS).lower()
    if 4 <= len(text) <= MAX_SHA_CHARS and all(char in _HEX_DIGITS for char in text):
        return text
    return ""


def _shas(value: Any) -> tuple[str, ...]:
    """Order-preserving, duplicate-free, bounded tuple of commit shas."""

    if isinstance(value, str):
        candidates: Iterable[Any] = value.split()
    elif isinstance(value, Mapping) or value is None:
        candidates = ()
    else:
        try:
            candidates = list(value)
        except TypeError:
            candidates = ()
    collected: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        sha = _sha(item)
        if not sha or sha in seen:
            continue
        seen.add(sha)
        collected.append(sha)
        if len(collected) >= MAX_PARENT_SHAS:
            break
    return tuple(collected)


def _numstat_value(value: Any) -> int | None:
    """Parse a ``--numstat`` number; ``None`` for binary (``-``) or garbage."""

    text = _text(value, limit=40)
    if not text or text == "-":
        return None
    try:
        number = int(text)
    except (TypeError, ValueError):
        return None
    return max(0, min(MAX_LINES_CHANGED_LIMIT, number))


def _clean_path(value: Any) -> str:
    """Normalize a reported path: unquote, resolve ``old => new``, bound it."""

    text = _text(value)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if "=>" in text:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start and " => " in text[start:end]:
            inner = text[start + 1 : end]
            destination = inner.split(" => ", 1)[1].strip()
            text = f"{text[:start]}{destination}{text[end + 1 :]}"
        else:
            text = text.rsplit(" => ", 1)[-1].strip()
    text = text.strip()
    while text.startswith("./"):
        text = text[2:]
    return _text(text)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitInfo:
    """One recently observed commit: identity, authorship and subject line."""

    sha: str
    parents: tuple[str, ...] = ()
    author: str = ""
    committed_at: str = ""
    subject: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha", _sha(self.sha))
        object.__setattr__(self, "parents", _shas(self.parents))
        object.__setattr__(self, "author", _text(self.author, limit=MAX_AUTHOR_CHARS))
        object.__setattr__(self, "committed_at", _text(self.committed_at, limit=40))
        object.__setattr__(self, "subject", _text(self.subject, limit=MAX_SUBJECT_CHARS))

    def short_sha(self, length: int = 12) -> str:
        """Abbreviated sha for log-safe identification."""

        return self.sha[: max(4, min(length, len(self.sha)))] if self.sha else ""

    def to_dict(self) -> dict:
        """JSON-safe projection of the commit metadata."""

        return {
            "sha": self.sha,
            "parents": list(self.parents),
            "author": self.author,
            "committed_at": self.committed_at,
            "subject": self.subject,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CommitInfo:
        """Rebuild commit metadata from stored data, tolerating missing fields."""

        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            sha=_sha(data.get("sha")),
            parents=_shas(data.get("parents")),
            author=_text(data.get("author"), limit=MAX_AUTHOR_CHARS),
            committed_at=_text(data.get("committed_at"), limit=40),
            subject=_text(data.get("subject"), limit=MAX_SUBJECT_CHARS),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: short sha, date and bounded subject."""

        return {
            "sha": self.short_sha(),
            "committed_at": self.committed_at,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class FileChurn:
    """How often one path changed and how many lines that change carried."""

    path: str
    commits: int = 0
    additions: int = 0
    deletions: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _clean_path(self.path))
        object.__setattr__(self, "commits", _count(self.commits))
        object.__setattr__(self, "additions", _count(self.additions))
        object.__setattr__(self, "deletions", _count(self.deletions))

    @property
    def lines_changed(self) -> int:
        """Total added plus deleted lines observed for the path."""

        return self.additions + self.deletions

    def to_dict(self) -> dict:
        """JSON-safe projection of the churn entry."""

        return {
            "path": self.path,
            "commits": self.commits,
            "additions": self.additions,
            "deletions": self.deletions,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FileChurn:
        """Rebuild one churn entry from stored data, tolerating missing fields."""

        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            path=data.get("path"),
            commits=_count(data.get("commits")),
            additions=_count(data.get("additions")),
            deletions=_count(data.get("deletions")),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: path identity plus bounded counters."""

        return {
            "path": self.path,
            "commits": self.commits,
            "lines_changed": self.lines_changed,
        }


@dataclass(frozen=True)
class GitDeepen:
    """A bounded view of recent history for one repository revision.

    ``recent_commits`` keeps git log order (newest first) and ``churn`` is
    ranked by commit count then changed lines. ``paths`` is the sorted union of
    tracked paths. ``truncated`` is true whenever a cap or the line budget
    stopped the parse, so consumers can tell a complete window from a clipped
    one.
    """

    base_ref: str = "HEAD"
    head: str = ""
    recent_commits: tuple[CommitInfo, ...] = ()
    churn: tuple[FileChurn, ...] = ()
    paths: tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_ref", _text(self.base_ref, limit=120) or "HEAD")
        object.__setattr__(self, "head", _sha(self.head))
        commits: list[CommitInfo] = []
        seen: set[str] = set()
        for item in tuple(self.recent_commits or ()):
            commit = item if isinstance(item, CommitInfo) else None
            if commit is None and isinstance(item, Mapping):
                commit = CommitInfo.from_dict(item)
            if commit is None or not commit.sha or commit.sha in seen:
                continue
            seen.add(commit.sha)
            commits.append(commit)
            if len(commits) >= MAX_COMMITS_LIMIT:
                break
        object.__setattr__(self, "recent_commits", tuple(commits))
        churn: list[FileChurn] = []
        churn_seen: set[str] = set()
        for item in tuple(self.churn or ()):
            entry = item if isinstance(item, FileChurn) else None
            if entry is None and isinstance(item, Mapping):
                entry = FileChurn.from_dict(item)
            if entry is None or not entry.path or entry.path in churn_seen:
                continue
            churn_seen.add(entry.path)
            churn.append(entry)
            if len(churn) >= MAX_PATHS_LIMIT:
                break
        churn.sort(key=lambda entry: (-entry.commits, -entry.lines_changed, entry.path))
        object.__setattr__(self, "churn", tuple(churn))
        paths: list[str] = []
        path_seen: set[str] = set()
        for item in tuple(self.paths or ()) + tuple(entry.path for entry in churn):
            path = _clean_path(item)
            if not path or path in path_seen:
                continue
            path_seen.add(path)
            paths.append(path)
            if len(paths) >= MAX_PATHS_LIMIT:
                break
        paths.sort()
        object.__setattr__(self, "paths", tuple(paths))
        object.__setattr__(self, "truncated", bool(self.truncated))

    @property
    def is_empty(self) -> bool:
        """True when no commit or path was observed for the window."""

        return not self.recent_commits and not self.churn

    def total_lines_changed(self) -> int:
        """Sum of added plus deleted lines across the tracked churn entries."""

        return sum(entry.lines_changed for entry in self.churn)

    def to_dict(self) -> dict:
        """JSON-safe projection of the deepening."""

        return {
            "base_ref": self.base_ref,
            "head": self.head,
            "recent_commits": [commit.to_dict() for commit in self.recent_commits],
            "churn": [entry.to_dict() for entry in self.churn],
            "paths": list(self.paths),
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> GitDeepen:
        """Rebuild a deepening from stored data, skipping malformed parts."""

        data = payload if isinstance(payload, Mapping) else {}
        commits = data.get("recent_commits") or ()
        churn = data.get("churn") or ()
        paths = data.get("paths") or ()
        return cls(
            base_ref=_text(data.get("base_ref"), limit=120) or "HEAD",
            head=_sha(data.get("head")),
            recent_commits=tuple(
                item for item in commits if isinstance(item, (CommitInfo, Mapping))
            ),
            churn=tuple(item for item in churn if isinstance(item, (FileChurn, Mapping))),
            paths=tuple(item for item in paths if isinstance(item, str)),
            truncated=bool(data.get("truncated")),
        )

    def to_summary(self) -> dict:
        """Log-safe summary: counts and revision identity only, never subjects."""

        return {
            "head": self.head[:12],
            "commits": len(self.recent_commits),
            "paths": len(self.paths),
            "lines_changed": self.total_lines_changed(),
            "truncated": self.truncated,
        }


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def _parse_commit_header(line: str) -> CommitInfo | None:
    """Parse one unit-separated commit header line, or return ``None``."""

    if _FIELD_SEP not in line:
        return None
    fields = line.split(_FIELD_SEP)
    sha = _sha(fields[0])
    if not sha:
        return None
    return CommitInfo(
        sha=sha,
        parents=_shas(fields[1] if len(fields) > 1 else ()),
        author=_text(fields[2] if len(fields) > 2 else "", limit=MAX_AUTHOR_CHARS),
        committed_at=_text(fields[3] if len(fields) > 3 else "", limit=40),
        subject=_text(
            _FIELD_SEP.join(fields[4:]) if len(fields) > 4 else "",
            limit=MAX_SUBJECT_CHARS,
        ),
    )


def _parse_numstat_line(line: str) -> tuple[str, int, int] | None:
    """Parse one ``--numstat`` line into ``(path, additions, deletions)``.

    Binary entries (``-``/``-``) are kept with zero line counts so the path is
    still counted as touched; malformed lines are skipped.
    """

    if "\t" not in line:
        return None
    parts = line.split("\t")
    if len(parts) < 3:
        return None
    path = _clean_path(parts[2] if len(parts) == 3 else "\t".join(parts[2:]))
    if not path:
        return None
    additions = _numstat_value(parts[0])
    deletions = _numstat_value(parts[1])
    return path, additions or 0, deletions or 0


def _churn_from_counts(counts: Mapping[str, list[int]]) -> tuple[FileChurn, ...]:
    """Build the deterministic churn ranking from accumulated counters."""

    entries = [
        FileChurn(
            path=path,
            commits=values[0],
            additions=values[1],
            deletions=values[2],
        )
        for path, values in counts.items()
    ]
    entries.sort(key=lambda entry: (-entry.commits, -entry.lines_changed, entry.path))
    return tuple(entries)


def parse_git_log_numstat(
    text: Any,
    *,
    max_commits: int = DEFAULT_MAX_COMMITS,
    max_paths: int = DEFAULT_MAX_PATHS,
    max_lines_changed: int = DEFAULT_MAX_LINES_CHANGED,
) -> tuple[tuple[CommitInfo, ...], tuple[FileChurn, ...], bool]:
    """Parse ``git log --numstat`` output into commits, churn and a truncation flag.

    The expected shape is what :func:`git_deepen` produces: NUL-terminated
    commit records whose header fields are unit-separated, each followed by its
    numstat block. Parsing is commit-atomic - a commit is either retained with
    all of its tracked numstat lines or dropped once a bound is reached, so
    churn is never a partial count for a retained commit. Blank lines, binary
    entries, renames and malformed lines are tolerated.
    """

    raw = text if isinstance(text, str) else ""
    commit_cap = _clamp(max_commits, default=DEFAULT_MAX_COMMITS, maximum=MAX_COMMITS_LIMIT)
    path_cap = _clamp(max_paths, default=DEFAULT_MAX_PATHS, maximum=MAX_PATHS_LIMIT)
    line_budget = _clamp(
        max_lines_changed, default=DEFAULT_MAX_LINES_CHANGED, maximum=MAX_LINES_CHANGED_LIMIT
    )
    commits: list[CommitInfo] = []
    counts: dict[str, list[int]] = {}
    lines_spent = 0
    truncated = False
    pending: CommitInfo | None = None
    entries: list[tuple[str, int, int]] = []

    def accept(commit: CommitInfo | None, changed: list[tuple[str, int, int]]) -> bool:
        """Retain one commit atomically; return false once a bound stops parsing."""

        nonlocal lines_spent, truncated
        if commit is None:
            return True
        if len(commits) >= commit_cap:
            truncated = True
            return False
        commit_lines = sum(additions + deletions for _path, additions, deletions in changed)
        if lines_spent + commit_lines > line_budget:
            truncated = True
            return False
        commits.append(commit)
        for path, additions, deletions in changed:
            values = counts.get(path)
            if values is None:
                if len(counts) >= path_cap:
                    truncated = True
                    continue
                values = [0, 0, 0]
                counts[path] = values
            values[0] += 1
            values[1] = min(MAX_LINES_CHANGED_LIMIT, values[1] + additions)
            values[2] = min(MAX_LINES_CHANGED_LIMIT, values[2] + deletions)
        lines_spent += commit_lines
        return True

    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        stripped = line.strip()
        if _FIELD_SEP in line:
            if not accept(pending, entries):
                pending = None
                entries = []
                break
            pending = _parse_commit_header(line)
            entries = []
            if pending is None:
                continue
        elif pending is not None:
            parsed = _parse_numstat_line(stripped)
            if parsed is not None:
                entries.append(parsed)
        # A numstat line before any header belongs to no observed commit: skip.
    else:
        if not accept(pending, entries):
            pending = None
            entries = []
    if truncated:
        return tuple(commits), _churn_from_counts(counts), True
    if pending is None and entries and not commits:
        truncated = False
    return tuple(commits), _churn_from_counts(counts), truncated


def parse_git_numstat(
    text: Any,
    *,
    max_paths: int = DEFAULT_MAX_PATHS,
    max_lines_changed: int = DEFAULT_MAX_LINES_CHANGED,
) -> tuple[FileChurn, ...]:
    """Parse a numstat block (no commit headers) into ranked file churn.

    Useful for fixtures and for one-off ``git diff --numstat`` output. Binary
    entries count as a touched path with zero lines; the path count and total
    changed lines are bounded.
    """

    raw = text if isinstance(text, str) else ""
    path_cap = _clamp(max_paths, default=DEFAULT_MAX_PATHS, maximum=MAX_PATHS_LIMIT)
    line_budget = _clamp(
        max_lines_changed, default=DEFAULT_MAX_LINES_CHANGED, maximum=MAX_LINES_CHANGED_LIMIT
    )
    counts: dict[str, list[int]] = {}
    lines_spent = 0
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        parsed = _parse_numstat_line(line.strip())
        if parsed is None:
            continue
        path, additions, deletions = parsed
        if path not in counts and len(counts) >= path_cap:
            continue
        values = counts.setdefault(path, [0, 0, 0])
        values[0] += 1
        values[1] = min(MAX_LINES_CHANGED_LIMIT, values[1] + additions)
        values[2] = min(MAX_LINES_CHANGED_LIMIT, values[2] + deletions)
        lines_spent += additions + deletions
        if lines_spent >= line_budget:
            break
    return _churn_from_counts(counts)


def parse_git_paths(text: Any, *, max_paths: int = DEFAULT_MAX_PATHS) -> tuple[str, ...]:
    """Parse newline-separated path output (``git ls-tree``) into bounded paths."""

    raw = text if isinstance(text, str) else ""
    path_cap = _clamp(max_paths, default=DEFAULT_MAX_PATHS, maximum=MAX_PATHS_LIMIT)
    paths: list[str] = []
    seen: set[str] = set()
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        path = _clean_path(line)
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= path_cap:
            break
    return tuple(paths)


def deepen_from_log(
    text: Any,
    *,
    base_ref: str = "HEAD",
    head: str = "",
    max_commits: int = DEFAULT_MAX_COMMITS,
    max_paths: int = DEFAULT_MAX_PATHS,
    max_lines_changed: int = DEFAULT_MAX_LINES_CHANGED,
) -> GitDeepen:
    """Build a :class:`GitDeepen` from captured ``git log --numstat`` output."""

    commits, churn, truncated = parse_git_log_numstat(
        text,
        max_commits=max_commits,
        max_paths=max_paths,
        max_lines_changed=max_lines_changed,
    )
    resolved_head = _sha(head) or (commits[0].sha if commits else "")
    return GitDeepen(
        base_ref=base_ref,
        head=resolved_head,
        recent_commits=commits,
        churn=churn,
        paths=tuple(entry.path for entry in churn),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Git execution (read-only)
# ---------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    """Child environment that keeps git non-interactive and side-effect free."""

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run_git(root: Path, args: list[str]) -> str | None:
    """Run one read-only git command; return stdout or ``None`` on any failure."""

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout or ""


def _resolve_commit(root: Path, base_ref: str) -> str:
    """Resolve ``base_ref`` to a commit sha, or ``""`` when it cannot be read."""

    output = _run_git(root, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
    if not output:
        return ""
    for line in output.splitlines():
        sha = _sha(line.strip())
        if sha:
            return sha
    return ""


def git_deepen(
    root: Any,
    *,
    base_ref: str = "HEAD",
    max_commits: int = DEFAULT_MAX_COMMITS,
    max_paths: int = DEFAULT_MAX_PATHS,
    max_lines_changed: int = DEFAULT_MAX_LINES_CHANGED,
) -> GitDeepen:
    """Deepen one repository revision with a bounded, read-only git history read.

    Runs ``git rev-parse`` and ``git log --numstat`` as argument vectors with a
    timeout; a missing root, a non-repository, a missing git binary or an
    invalid ``base_ref`` yields an empty deepening (``base_ref`` preserved)
    instead of raising. No command outside the read-only allow-list is used and
    the repository is never modified.
    """

    base = _text(base_ref, limit=120) or "HEAD"
    root_path = Path(root)
    if not root_path.is_dir():
        return GitDeepen(base_ref=base)
    commit_cap = _clamp(max_commits, default=DEFAULT_MAX_COMMITS, maximum=MAX_COMMITS_LIMIT)
    log_output = _run_git(
        root_path,
        [
            "log",
            f"-n{commit_cap + 1}",
            "--numstat",
            f"--format={_LOG_FORMAT}",
            base,
        ],
    )
    if log_output is None:
        return GitDeepen(base_ref=base)
    head = _resolve_commit(root_path, base)
    return deepen_from_log(
        log_output,
        base_ref=base,
        head=head,
        max_commits=commit_cap,
        max_paths=max_paths,
        max_lines_changed=max_lines_changed,
    )


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _as_deepen(value: Any) -> GitDeepen:
    """Coerce a deepening or stored mapping into a :class:`GitDeepen`."""

    if isinstance(value, GitDeepen):
        return value
    if isinstance(value, Mapping):
        return GitDeepen.from_dict(value)
    return GitDeepen()


def _scope_key(scope: Any) -> str:
    """Best-effort stable scope key without importing the scope type."""

    if scope is None:
        return ""
    key = getattr(scope, "key", None)
    if callable(key):
        try:
            return _text(key(), limit=240)
        except (TypeError, ValueError):
            return ""
    if isinstance(scope, Mapping):
        key = scope.get("key")
        if isinstance(key, str):
            return _text(key, limit=240)
        return ":".join(
            _text(scope.get(field), limit=80)
            for field in ("tenant_id", "repo_id", "branch")
        )
    return ""


def summarize_deepen(deepen: Any) -> dict:
    """Log-safe commit/path/line counts of a deepening.

    Accepts ``None``, a :class:`GitDeepen`, or a mapping of pre-computed counts
    (``commits``/``paths``/``lines_changed``/``truncated``). Never returns
    subjects, authors or paths, so the result may be logged or emitted in an
    event.
    """

    summary = {
        "commits": 0,
        "paths": 0,
        "lines_changed": 0,
        "truncated": False,
    }
    if deepen is None:
        return summary
    if isinstance(deepen, Mapping) and isinstance(
        deepen.get("commits"), (int, float)
    ) and not isinstance(deepen.get("commits"), bool):
        summary["commits"] = _count(deepen.get("commits"))
        summary["paths"] = _count(deepen.get("paths"))
        summary["lines_changed"] = _count(deepen.get("lines_changed"))
        summary["truncated"] = bool(deepen.get("truncated"))
        return summary
    model = _as_deepen(deepen)
    return {
        "commits": len(model.recent_commits),
        "paths": len(model.paths),
        "lines_changed": model.total_lines_changed(),
        "truncated": model.truncated,
    }


def deepen_for_scope(
    deepen: Any,
    *,
    scope: Any,
    max_commits: int = 10,
    max_paths: int = 20,
) -> dict:
    """Bounded metadata projection of a deepening for one knowledge scope.

    Git history carries no tenant/repository identifier, so this function never
    invents attribution: it copies the bounded facts it was given and labels
    them with the caller's scope key. Commit subjects and paths are repository
    metadata, never file contents; use :func:`summarize_deepen` for a fully
    log-safe view.
    """

    model = _as_deepen(deepen)
    commit_cap = _clamp(max_commits, default=10, maximum=MAX_COMMITS_LIMIT)
    path_cap = _clamp(max_paths, default=20, maximum=MAX_PATHS_LIMIT)
    return {
        "scope": _scope_key(scope),
        "base_ref": model.base_ref,
        "head": model.head[:12],
        "commit_count": len(model.recent_commits),
        "commits": [commit.to_summary() for commit in model.recent_commits[:commit_cap]],
        "path_count": len(model.paths),
        "paths": list(model.paths[:path_cap]),
        "lines_changed": model.total_lines_changed(),
        "truncated": model.truncated,
    }
