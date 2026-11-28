"""GrepMem-style append-only memory and versioned precomputed views.

Ledger item #133: GrepMem keeps a lossless append-only text log and retrieves
it with regular expressions, without an embedding model or a vector index.
Memorix pins project context to an immutable repository commit so a later run
can reuse the exact view that was built for that tree.  SELD-style evidence
loss detection flags terms that a lossy summary silently dropped.

The module is deterministic bookkeeping only.  Callers own storage and supply
the raw log lines and summaries; nothing here calls a model or a database.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "GREPMEM_MEMORIX_ENV",
    "LogEntry",
    "GrepMemStore",
    "VersionedView",
    "VersionedViewStore",
    "EvidenceLossReport",
    "detect_evidence_loss",
    "RefusalCompressionGuard",
    "GrepMemMemorix",
    "grepmem_memorix_enabled",
]

GREPMEM_MEMORIX_ENV = "UC_GREPMEM_MEMORIX"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def grepmem_memorix_enabled() -> bool:
    """Return whether the opt-in GrepMem/Memorix path is enabled."""

    return os.environ.get(GREPMEM_MEMORIX_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class LogEntry:
    """One lossless line in the append-only task log."""

    seq: int
    text: str
    source: str = "agent"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if not self.source.strip():
            raise ValueError("source must not be empty")


class GrepMemStore:
    """Append-only log queried with regular expressions."""

    def __init__(self, max_entries: int = 100_000) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: list[LogEntry] = []
        self._next_seq = 0

    def append(self, text: str, source: str = "agent") -> LogEntry:
        if len(self._entries) >= self._max_entries:
            raise ValueError("log capacity reached")
        entry = LogEntry(self._next_seq, text, source)
        self._entries.append(entry)
        self._next_seq += 1
        return entry

    def query(self, pattern: str, *, limit: int = 50) -> tuple[LogEntry, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        try:
            regex = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"invalid regular expression: {error}") from error
        hits = [entry for entry in self._entries if regex.search(entry.text)]
        return tuple(hits[:limit])

    def all(self) -> tuple[LogEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class VersionedView:
    """A precomputed project view pinned to one repository commit."""

    commit: str
    name: str
    content: str

    def __post_init__(self) -> None:
        if not self.commit.strip():
            raise ValueError("commit must not be empty")
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if not self.content.strip():
            raise ValueError("content must not be empty")


class VersionedViewStore:
    """Immutable commit -> named view store, with an explicit build cache."""

    def __init__(self, max_views: int = 256) -> None:
        if max_views <= 0:
            raise ValueError("max_views must be positive")
        self._max_views = max_views
        self._views: dict[tuple[str, str], VersionedView] = {}
        self._builds = 0

    def put(self, view: VersionedView) -> None:
        key = (view.commit, view.name)
        if key not in self._views:
            if len(self._views) >= self._max_views:
                raise ValueError("view capacity reached")
            self._builds += 1
        self._views[key] = view

    def get(self, commit: str, name: str) -> VersionedView | None:
        return self._views.get((commit, name))

    def list_for_commit(self, commit: str) -> tuple[VersionedView, ...]:
        return tuple(
            view
            for (pinned_commit, _), view in sorted(self._views.items())
            if pinned_commit == commit
        )

    def build_count(self) -> int:
        return self._builds


@dataclass(frozen=True)
class EvidenceLossReport:
    """Terms lost by a compressed representation of an evidence set."""

    required: tuple[str, ...]
    missing: tuple[str, ...]
    silent_loss: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "required": list(self.required),
            "missing": list(self.missing),
            "silent_loss": self.silent_loss,
        }


def detect_evidence_loss(
    source: str, retained: str, required_terms: Iterable[str]
) -> EvidenceLossReport:
    """Report required terms present in the source but absent from the view."""

    required = tuple(term for term in required_terms if term.strip())
    missing = tuple(
        term for term in required if term in source and term not in retained
    )
    return EvidenceLossReport(required, missing, bool(missing))


class RefusalCompressionGuard:
    """Escalate a compressed memory that would cause an unnecessary refusal."""

    def __init__(self, refusal_markers: Iterable[str] = ("I don't know", "cannot find")) -> None:
        self._markers = tuple(marker.lower() for marker in refusal_markers if marker.strip())

    def should_escalate(self, compressed: str, raw: str) -> bool:
        lowered = compressed.lower()
        return any(marker in lowered for marker in self._markers) and bool(raw.strip())


class GrepMemMemorix:
    """Thin facade over the append-only log and versioned views."""

    def __init__(
        self,
        log: GrepMemStore | None = None,
        views: VersionedViewStore | None = None,
        guard: RefusalCompressionGuard | None = None,
    ) -> None:
        self.log = log if log is not None else GrepMemStore()
        self.views = views if views is not None else VersionedViewStore()
        self.guard = guard if guard is not None else RefusalCompressionGuard()

    def remember(self, text: str, source: str = "agent") -> LogEntry:
        return self.log.append(text, source)

    def recall(self, pattern: str, *, limit: int = 20) -> tuple[LogEntry, ...]:
        return self.log.query(pattern, limit=limit)

    def pin_view(self, commit: str, name: str, content: str) -> VersionedView:
        view = VersionedView(commit, name, content)
        self.views.put(view)
        return view

    def view_for(self, commit: str, name: str) -> VersionedView | None:
        return self.views.get(commit, name)

    def audit(self, compressed: str, raw: str, required: Iterable[str]) -> EvidenceLossReport:
        return detect_evidence_loss(raw, compressed, required)
