"""MEMOIR training-free memory lifecycle (ledger item #110, UC_MEMOIR_LIFECYCLE).

Implements query-time attribution plus UCB exploration and an offline
judge-gated merge. All operations are local and deterministic; no model is
called.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

MEMOIR_LIFECYCLE_ENV = "UC_MEMOIR_LIFECYCLE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def memoir_lifecycle_enabled() -> bool:
    return os.environ.get(MEMOIR_LIFECYCLE_ENV, "").strip().lower() in _TRUTHY


class MemoirError(ValueError):
    """Raised for malformed memory records."""


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


@dataclass(frozen=True)
class MemoryItem:
    id: str
    content: str
    embedding: tuple[float, ...]
    utility: float = 0.0
    exposures: int = 0

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.content.strip():
            raise MemoirError("memory id and content must not be empty")
        if not self.embedding:
            raise MemoirError("embedding must not be empty")


@dataclass(frozen=True)
class MergeDecision:
    merged: bool
    primary_id: str
    secondary_id: str
    evidence_lost: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "merged": self.merged,
            "primary_id": self.primary_id,
            "secondary_id": self.secondary_id,
            "evidence_lost": list(self.evidence_lost),
        }


@dataclass(frozen=True)
class MemoirReport:
    stored: int
    merged: int
    evidence_lost: int
    next_exploration: float

    def to_dict(self) -> dict[str, object]:
        return {
            "stored": self.stored,
            "merged": self.merged,
            "evidence_lost": self.evidence_lost,
            "next_exploration": self.next_exploration,
        }


class MemoirLifecycle:
    def __init__(self, exploration: float = 0.5, merge_threshold: float = 0.95) -> None:
        if exploration < 0:
            raise MemoirError("exploration must not be negative")
        if not 0.0 <= merge_threshold <= 1.0:
            raise MemoirError("merge_threshold must be in [0, 1]")
        self.exploration = exploration
        self.merge_threshold = merge_threshold
        self._items: dict[str, MemoryItem] = {}
        self._query_count = 0

    def add(self, item: MemoryItem) -> None:
        if item.id in self._items:
            raise MemoirError(f"duplicate memory id: {item.id}")
        self._items[item.id] = item

    def items(self) -> tuple[MemoryItem, ...]:
        return tuple(self._items[key] for key in sorted(self._items))

    def _score(self, item: MemoryItem, query: tuple[float, ...]) -> float:
        base = _cosine(item.embedding, query)
        exploration = self.exploration * math.sqrt(
            math.log(self._query_count + 1.0) / (item.exposures + 1.0)
        )
        return base + exploration + item.utility

    def retrieve(self, query: tuple[float, ...], k: int = 5) -> tuple[MemoryItem, ...]:
        if k < 1:
            raise MemoirError("k must be positive")
        self._query_count += 1
        ranked = sorted(
            self._items.values(),
            key=lambda item: (-self._score(item, query), item.id),
        )[:k]
        for item in ranked:
            self._items[item.id] = MemoryItem(
                id=item.id,
                content=item.content,
                embedding=item.embedding,
                utility=item.utility,
                exposures=item.exposures + 1,
            )
        return tuple(self._items[item.id] for item in ranked)

    def record_utility(self, memory_id: str, reward: float) -> None:
        if memory_id not in self._items:
            raise MemoirError(f"unknown memory id: {memory_id}")
        item = self._items[memory_id]
        new_utility = item.utility * 0.5 + reward * 0.5
        self._items[memory_id] = MemoryItem(
            id=item.id,
            content=item.content,
            embedding=item.embedding,
            utility=round(new_utility, 6),
            exposures=item.exposures,
        )

    def merge_candidates(self) -> tuple[tuple[str, str], ...]:
        values = self.items()
        pairs = []
        for index, left in enumerate(values):
            for right in values[index + 1 :]:
                if _cosine(left.embedding, right.embedding) >= self.merge_threshold:
                    pairs.append((left.id, right.id))
        return tuple(pairs)

    def judge_merge(self, primary_id: str, secondary_id: str) -> MergeDecision:
        if primary_id not in self._items or secondary_id not in self._items:
            raise MemoirError("unknown memory id for merge")
        primary = self._items[primary_id]
        secondary = self._items[secondary_id]
        primary_words = set(primary.content.split())
        secondary_words = set(secondary.content.split())
        missing = tuple(sorted(secondary_words - primary_words))
        if missing:
            return MergeDecision(False, primary_id, secondary_id, missing)
        del self._items[secondary_id]
        return MergeDecision(True, primary_id, secondary_id, ())

    def sleep_consolidate(self) -> MemoirReport:
        merged = 0
        lost = 0
        for primary_id, secondary_id in self.merge_candidates():
            decision = self.judge_merge(primary_id, secondary_id)
            if decision.merged:
                merged += 1
            else:
                lost += len(decision.evidence_lost)
        return MemoirReport(
            stored=len(self._items),
            merged=merged,
            evidence_lost=lost,
            next_exploration=round(self.exploration * 0.95, 4),
        )

    def stats(self) -> dict[str, object]:
        return {"stored": len(self._items), "queries": self._query_count}


__all__ = [
    "MEMOIR_LIFECYCLE_ENV",
    "MemoirError",
    "MemoirLifecycle",
    "MemoirReport",
    "MemoryItem",
    "MergeDecision",
    "memoir_lifecycle_enabled",
]
