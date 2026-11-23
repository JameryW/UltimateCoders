"""STAIR hierarchical trajectory abstraction (#74).

Historical repair trajectories are represented as a multi-level plan tree:

* strategy nodes capture the high-level repair approach;
* subgoal nodes capture module-level steps;
* diagnostic nodes capture concrete search and inspection actions.

For a new issue the retriever mixes nodes from several levels and renders a
bounded, executable plan.  The module is pure bookkeeping over caller-supplied
trajectories; it does not inspect a repository itself.

Flag: ``UC_STAIR_TREE`` (default off).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "STAIR_TREE_ENV",
    "StairNode",
    "StairPlan",
    "StairTree",
    "TrajectoryLevel",
    "stair_tree_enabled",
]

STAIR_TREE_ENV = "UC_STAIR_TREE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def stair_tree_enabled() -> bool:
    """Return whether STAIR retrieval is enabled."""

    return os.environ.get(STAIR_TREE_ENV, "").strip().lower() in _TRUTHY


def _words(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[A-Za-z0-9_]+", text) if part}


def _require(value: str, field: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


class TrajectoryLevel(str, Enum):
    """Granularity of a node in the hierarchical plan tree."""

    STRATEGY = "strategy"
    SUBGOAL = "subgoal"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class StairNode:
    """One node of the hierarchical plan tree."""

    id: str
    level: TrajectoryLevel
    text: str
    tags: tuple[str, ...] = ()
    parent_id: str | None = None
    success: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require(self.id, "id"))
        object.__setattr__(self, "text", _require(self.text, "text"))
        object.__setattr__(
            self,
            "tags",
            tuple(tag.strip() for tag in self.tags if tag.strip()),
        )


@dataclass(frozen=True)
class StairPlan:
    """A mixed-granularity plan selected for a new issue."""

    strategy: StairNode | None
    subgoals: tuple[StairNode, ...]
    diagnostics: tuple[StairNode, ...]
    score: float

    @property
    def nodes(self) -> tuple[StairNode, ...]:
        items: list[StairNode] = []
        if self.strategy is not None:
            items.append(self.strategy)
        items.extend(self.subgoals)
        items.extend(self.diagnostics)
        return tuple(items)

    def render(self) -> str:
        lines: list[str] = []
        if self.strategy is not None:
            lines.append(f"Strategy: {self.strategy.text}")
        for index, node in enumerate(self.subgoals, 1):
            lines.append(f"Subgoal {index}: {node.text}")
        for index, node in enumerate(self.diagnostics, 1):
            lines.append(f"Diagnostic {index}: {node.text}")
        return "\n".join(lines)


class StairTree:
    """In-memory hierarchy of successful and failed repair trajectories."""

    def __init__(self) -> None:
        self._nodes: dict[str, StairNode] = {}
        self._counter = 0

    def add_node(
        self,
        level: TrajectoryLevel,
        text: str,
        *,
        tags: tuple[str, ...] = (),
        parent_id: str | None = None,
        success: bool = True,
    ) -> StairNode:
        if parent_id is not None and parent_id not in self._nodes:
            raise ValueError(f"unknown parent node: {parent_id}")
        self._counter += 1
        node = StairNode(
            f"n{self._counter}",
            level,
            text,
            tags=tags,
            parent_id=parent_id,
            success=success,
        )
        self._nodes[node.id] = node
        return node

    def add_trajectory(
        self,
        strategy: str,
        subgoals: tuple[str, ...] | list[str],
        diagnostics: tuple[str, ...] | list[str],
        *,
        tags: tuple[str, ...] = (),
        success: bool = True,
    ) -> tuple[StairNode, ...]:
        root = self.add_node(
            TrajectoryLevel.STRATEGY, strategy, tags=tags, success=success
        )
        created: list[StairNode] = [root]
        for subgoal in subgoals:
            created.append(
                self.add_node(
                    TrajectoryLevel.SUBGOAL,
                    subgoal,
                    tags=tags,
                    parent_id=root.id,
                    success=success,
                )
            )
        for diagnostic in diagnostics:
            created.append(
                self.add_node(
                    TrajectoryLevel.DIAGNOSTIC,
                    diagnostic,
                    tags=tags,
                    parent_id=root.id,
                    success=success,
                )
            )
        return tuple(created)

    def nodes(self) -> tuple[StairNode, ...]:
        return tuple(self._nodes.values())

    def search(
        self,
        query: str,
        *,
        tags: tuple[str, ...] = (),
        max_nodes: int = 8,
        include_failed: bool = False,
    ) -> StairPlan:
        if max_nodes < 1:
            raise ValueError("max_nodes must be positive")
        query_words = _words(query)
        tag_set = {tag.lower() for tag in tags}

        def score(node: StairNode) -> float:
            text_words = _words(node.text)
            overlap = len(query_words & text_words)
            tag_overlap = len(tag_set & {tag.lower() for tag in node.tags})
            return float(overlap * 2 + tag_overlap)

        candidates = [
            node
            for node in self._nodes.values()
            if include_failed or node.success
        ]
        strategies = sorted(
            (node for node in candidates if node.level is TrajectoryLevel.STRATEGY),
            key=lambda node: (-score(node), node.id),
        )
        subgoals = sorted(
            (node for node in candidates if node.level is TrajectoryLevel.SUBGOAL),
            key=lambda node: (-score(node), node.id),
        )
        diagnostics = sorted(
            (node for node in candidates if node.level is TrajectoryLevel.DIAGNOSTIC),
            key=lambda node: (-score(node), node.id),
        )

        strategy = strategies[0] if strategies else None
        remaining = max_nodes - (1 if strategy is not None else 0)
        subgoal_count = min(len(subgoals), max(1, remaining // 2)) if remaining else 0
        chosen_subgoals = tuple(subgoals[:subgoal_count])
        remaining -= subgoal_count
        chosen_diagnostics = tuple(diagnostics[: max(0, remaining)])
        total = (score(strategy) if strategy else 0.0) + sum(
            score(node) for node in chosen_subgoals + chosen_diagnostics
        )
        return StairPlan(strategy, chosen_subgoals, chosen_diagnostics, total)
