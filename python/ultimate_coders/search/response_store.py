"""External response store and discovery database (ledger item #88).

GeoFaham's response store keeps large execution artifacts outside the LLM
context window: the agent exchanges compact handles and summaries, not raw
logs or flamegraphs. AgentDiscover's discovery database is modelled as one
relational candidate table that can answer MAP-Elites and MCTS-style queries
without a separate search loop. Everything here is deterministic and local.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "RESPONSE_STORE_ENV",
    "ResponseStoreError",
    "ArtifactKind",
    "ArtifactHandle",
    "ResponseStore",
    "Candidate",
    "DiscoveryDatabase",
    "response_store_enabled",
]

RESPONSE_STORE_ENV = "UC_RESPONSE_STORE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def response_store_enabled() -> bool:
    return os.environ.get(RESPONSE_STORE_ENV, "").strip().lower() in _TRUTHY


class ResponseStoreError(ValueError):
    """Raised when an artifact handle or query is malformed."""


class ArtifactKind(str, Enum):
    TEST_LOG = "test_log"
    FLAMEGRAPH = "flamegraph"
    BUILD_OUTPUT = "build_output"
    CODE_SNIPPET = "code_snippet"
    OTHER = "other"


@dataclass(frozen=True)
class ArtifactHandle:
    artifact_id: str
    kind: ArtifactKind
    summary: str
    tokens: int
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ResponseStoreError("artifact id must not be empty")
        if not self.summary.strip():
            raise ResponseStoreError("artifact summary must not be empty")
        if self.tokens < 0:
            raise ResponseStoreError("token count must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.artifact_id,
            "kind": self.kind.value,
            "summary": self.summary,
            "tokens": self.tokens,
            "metadata": dict(self.metadata),
        }


@dataclass
class ResponseStore:
    """Content-addressed external artifact store with compact handles."""

    _items: dict[str, tuple[ArtifactHandle, str]] = field(default_factory=dict)
    _next: int = 1

    def put(
        self,
        kind: ArtifactKind,
        content: str,
        *,
        summary: str = "",
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactHandle:
        if not isinstance(kind, ArtifactKind):
            raise ResponseStoreError("kind must be an ArtifactKind")
        if not content.strip():
            raise ResponseStoreError("artifact content must not be empty")
        handle = ArtifactHandle(
            artifact_id=f"artifact-{self._next}",
            kind=kind,
            summary=summary or f"{kind.value} artifact",
            tokens=max(1, len(content.split())),
            metadata=dict(metadata or {}),
        )
        self._next += 1
        self._items[handle.artifact_id] = (handle, content)
        return handle

    def get(self, handle: ArtifactHandle | str) -> str:
        artifact_id = handle.artifact_id if isinstance(handle, ArtifactHandle) else handle
        entry = self._items.get(artifact_id)
        if entry is None:
            raise ResponseStoreError(f"unknown artifact: {artifact_id}")
        return entry[1]

    def render(self, handle: ArtifactHandle | str, max_chars: int = 800) -> str:
        if max_chars <= 0:
            raise ResponseStoreError("max_chars must be positive")
        content = self.get(handle)
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + "\n...[truncated]"

    def query(
        self,
        *,
        kind: ArtifactKind | None = None,
        metadata: Mapping[str, str] | None = None,
        limit: int = 20,
    ) -> tuple[ArtifactHandle, ...]:
        if limit <= 0:
            raise ResponseStoreError("limit must be positive")
        results: list[ArtifactHandle] = []
        for handle, _ in self._items.values():
            if kind is not None and handle.kind != kind:
                continue
            if metadata and any(
                handle.metadata.get(key) != value for key, value in metadata.items()
            ):
                continue
            results.append(handle)
            if len(results) >= limit:
                break
        return tuple(results)

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    cell: str
    score: float
    payload: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ResponseStoreError("candidate id must not be empty")
        if not self.cell.strip():
            raise ResponseStoreError("candidate cell must not be empty")
        if not 0.0 <= self.score <= 1.0:
            raise ResponseStoreError("candidate score must be in [0, 1]")


@dataclass
class DiscoveryDatabase:
    """A single candidate table supporting cell, score and frontier queries."""

    _candidates: dict[str, Candidate] = field(default_factory=dict)

    def add(self, candidate: Candidate) -> None:
        if not isinstance(candidate, Candidate):
            raise ResponseStoreError("candidate must be a Candidate")
        previous = self._candidates.get(candidate.candidate_id)
        if previous is not None and previous != candidate:
            raise ResponseStoreError(f"duplicate candidate id: {candidate.candidate_id}")
        self._candidates[candidate.candidate_id] = candidate

    def best(self, cell: str) -> Candidate | None:
        matches = [item for item in self._candidates.values() if item.cell == cell]
        if not matches:
            return None
        return max(matches, key=lambda item: (item.score, item.candidate_id))

    def frontier(self, limit: int = 20) -> tuple[Candidate, ...]:
        if limit <= 0:
            raise ResponseStoreError("limit must be positive")
        cells: dict[str, Candidate] = {}
        for item in self._candidates.values():
            current = cells.get(item.cell)
            if current is None or (item.score, item.candidate_id) > (
                current.score,
                current.candidate_id,
            ):
                cells[item.cell] = item
        ordered = sorted(cells.values(), key=lambda item: (-item.score, item.cell))
        return tuple(ordered[:limit])

    def query(
        self,
        *,
        cell: str | None = None,
        min_score: float = 0.0,
        limit: int = 20,
    ) -> tuple[Candidate, ...]:
        if not 0.0 <= min_score <= 1.0:
            raise ResponseStoreError("min_score must be in [0, 1]")
        if limit <= 0:
            raise ResponseStoreError("limit must be positive")
        results = [
            item
            for item in self._candidates.values()
            if (cell is None or item.cell == cell) and item.score >= min_score
        ]
        results.sort(key=lambda item: (-item.score, item.candidate_id))
        return tuple(results[:limit])

    def __len__(self) -> int:
        return len(self._candidates)
