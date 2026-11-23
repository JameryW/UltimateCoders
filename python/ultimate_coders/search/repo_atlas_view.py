"""RepoAtlas (#39) — evolving multimodal repository views.

The controller implements the paper's *Select-Project-Refresh* loop:

* before the first substantive edit it projects a topology view plus a compact
  source index with stable reference keys;
* after the first substantive edit it unloads the topology view and switches to
  a plain-text neighbourhood around the active patch;
* every refresh replaces the previous projection instead of appending to it, so
  multimodal tokens do not accumulate across turns.

The graph is caller-supplied; this module performs no parsing, I/O or model
call. Ledger item: #39 (arXiv:2609.16936).
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AtlasSelection",
    "REPO_ATLAS_VIEW_ENABLED_ENV",
    "RepoAtlasController",
    "RepoGraph",
    "RepoNode",
    "ViewKind",
    "ViewProjection",
    "repo_atlas_view_enabled",
]

REPO_ATLAS_VIEW_ENABLED_ENV = "UC_REPO_ATLAS_VIEW"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def repo_atlas_view_enabled() -> bool:
    """Whether the opt-in RepoAtlas controller is enabled."""
    return (
        os.environ.get(REPO_ATLAS_VIEW_ENABLED_ENV, "").strip().lower() in _TRUTHY
    )


class ViewKind(str, Enum):
    """Which projection is currently in the prompt."""

    TOPOLOGY = "topology"
    SOURCE_INDEX = "source_index"
    LOCAL_TEXT = "local_text"


@dataclass(frozen=True)
class RepoNode:
    """One node of the caller-supplied repository graph."""

    id: str
    path: str
    symbols: tuple[str, ...] = ()
    signatures: tuple[str, ...] = ()
    edges: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("node id must not be empty")
        if not self.path.strip():
            raise ValueError("node path must not be empty")
        symbols = tuple(str(symbol) for symbol in self.symbols)
        signatures = tuple(str(signature) for signature in self.signatures)
        edges = tuple(str(edge) for edge in self.edges)
        if any(not symbol.strip() for symbol in symbols):
            raise ValueError("node symbols must not be empty strings")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "signatures", signatures)
        object.__setattr__(self, "edges", edges)


@dataclass(frozen=True)
class RepoGraph:
    """Directed graph of repository nodes with stable ids."""

    nodes: tuple[RepoNode, ...]

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        if not nodes:
            raise ValueError("repository graph must contain at least one node")
        by_id: dict[str, RepoNode] = {}
        for node in nodes:
            if node.id in by_id:
                raise ValueError(f"duplicate repository node id: {node.id}")
            by_id[node.id] = node
        for node in nodes:
            for edge in node.edges:
                if edge not in by_id:
                    raise ValueError(
                        f"node {node.id!r} references unknown edge {edge!r}"
                    )
                if edge == node.id:
                    raise ValueError(f"node {node.id!r} must not reference itself")
        object.__setattr__(self, "nodes", nodes)

    def node(self, node_id: str) -> RepoNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    def neighbors(self, node_id: str, *, hops: int = 1) -> tuple[str, ...]:
        """Breadth-first neighbours up to ``hops``; deterministic graph order."""
        if hops <= 0:
            return ()
        by_id = {node.id: node for node in self.nodes}
        frontier = [node_id]
        seen = {node_id}
        ordered: list[str] = []
        for _ in range(hops):
            next_frontier: list[str] = []
            for current in frontier:
                for edge in by_id[current].edges:
                    if edge not in seen:
                        seen.add(edge)
                        ordered.append(edge)
                        next_frontier.append(edge)
            frontier = next_frontier
        return tuple(ordered)

    def path_map(self) -> dict[str, str]:
        return {node.path: node.id for node in self.nodes}


@dataclass(frozen=True)
class ViewProjection:
    """One rendered view and its bounded token estimate."""

    kind: ViewKind
    refs: tuple[str, ...]
    rendered: str
    token_estimate: int
    degraded: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "refs": list(self.refs),
            "token_estimate": self.token_estimate,
            "degraded": self.degraded,
        }


@dataclass(frozen=True)
class AtlasSelection:
    """The current (not historical) projection set for one turn."""

    views: tuple[ViewProjection, ...]
    reason: str
    refresh_count: int
    replaced_previous: bool
    substantive_edit: bool
    active_paths: tuple[str, ...]

    @property
    def refs(self) -> tuple[str, ...]:
        seen: list[str] = []
        for view in self.views:
            for ref in view.refs:
                if ref not in seen:
                    seen.append(ref)
        return tuple(seen)

    def to_dict(self) -> dict[str, object]:
        return {
            "views": [view.to_dict() for view in self.views],
            "reason": self.reason,
            "refresh_count": self.refresh_count,
            "replaced_previous": self.replaced_previous,
            "substantive_edit": self.substantive_edit,
            "active_paths": list(self.active_paths),
        }


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class RepoAtlasController:
    """Select-Project-Refresh controller over a caller-supplied graph."""

    def __init__(self, graph: RepoGraph, *, budget_tokens: int = 4000) -> None:
        if budget_tokens <= 0:
            raise ValueError("budget_tokens must be positive")
        self.graph = graph
        self.budget_tokens = budget_tokens
        self._refresh_count = 0
        self._current: tuple[ViewProjection, ...] = ()
        self._edit_active = False

    @property
    def refresh_count(self) -> int:
        return self._refresh_count

    @property
    def current(self) -> tuple[ViewProjection, ...]:
        return self._current

    def select(
        self,
        *,
        substantive_edit: bool = False,
        active_paths: Sequence[str] = (),
        seed: str | None = None,
    ) -> AtlasSelection:
        """Return the current view set and replace any previous projection."""
        active = tuple(str(path) for path in active_paths)
        if substantive_edit:
            self._edit_active = True
        replaced = bool(self._current)
        if self._edit_active:
            views = (self.project_local_text(active, seed=seed),)
            reason = "post-edit-local-text-neighbourhood"
        else:
            topology = self.project_topology(seed or self.graph.nodes[0].id, hops=2)
            index = self.project_source_index(topology.refs)
            views = (topology, index)
            reason = "pre-edit-dual-topology-and-source-index"
        self._current = views
        self._refresh_count += 1
        return AtlasSelection(
            views=views,
            reason=reason,
            refresh_count=self._refresh_count,
            replaced_previous=replaced,
            substantive_edit=self._edit_active,
            active_paths=active,
        )

    def _fit(self, rendered: str) -> tuple[str, int, bool]:
        budget_chars = self.budget_tokens * 4
        if len(rendered) <= budget_chars:
            return rendered, _tokens(rendered), False
        trimmed = rendered[: max(1, budget_chars)]
        return trimmed, _tokens(trimmed), True

    def project_topology(self, seed: str, *, hops: int = 2) -> ViewProjection:
        """Project a connected topology view around ``seed``."""
        if seed not in self.graph.ids():
            raise KeyError(seed)
        ids = (seed, *self.graph.neighbors(seed, hops=hops))
        lines = ["topology"]
        for node_id in ids:
            node = self.graph.node(node_id)
            lines.append(f"- {node.id} | {node.path}")
            if node.symbols:
                lines.append(f"  symbols: {', '.join(node.symbols)}")
            if node.edges:
                lines.append(f"  edges: {', '.join(node.edges)}")
        rendered, tokens, degraded = self._fit("\n".join(lines))
        return ViewProjection(
            kind=ViewKind.TOPOLOGY,
            refs=ids,
            rendered=rendered,
            token_estimate=tokens,
            degraded=degraded,
        )

    def project_source_index(
        self,
        refs: Iterable[str] | None = None,
    ) -> ViewProjection:
        """Project a compact source index keyed by stable node ids."""
        ids = tuple(refs) if refs is not None else self.graph.ids()
        for node_id in ids:
            if node_id not in self.graph.ids():
                raise KeyError(node_id)
        lines = ["source_index"]
        for node_id in ids:
            node = self.graph.node(node_id)
            symbols = ", ".join(node.symbols) or "-"
            signature = " | ".join(node.signatures) or "-"
            lines.append(f"ref {node.id} | {node.path} | {symbols} | {signature}")
        rendered, tokens, degraded = self._fit("\n".join(lines))
        return ViewProjection(
            kind=ViewKind.SOURCE_INDEX,
            refs=ids,
            rendered=rendered,
            token_estimate=tokens,
            degraded=degraded,
        )

    def project_local_text(
        self,
        active_paths: Sequence[str],
        *,
        seed: str | None = None,
        hops: int = 1,
    ) -> ViewProjection:
        """Project only the plain-text neighbourhood of the active patch."""
        path_to_id = self.graph.path_map()
        active_ids = [path_to_id[path] for path in active_paths if path in path_to_id]
        if not active_ids:
            active_ids = [seed or self.graph.nodes[0].id]
        ordered: list[str] = []
        for node_id in active_ids:
            if node_id not in ordered:
                ordered.append(node_id)
            for neighbor in self.graph.neighbors(node_id, hops=hops):
                if neighbor not in ordered:
                    ordered.append(neighbor)
        lines = ["local_text"]
        for node_id in ordered:
            node = self.graph.node(node_id)
            lines.append(f"{node.path}")
            if node.symbols:
                lines.append(f"  symbols: {', '.join(node.symbols)}")
            if node.signatures:
                lines.append(f"  signatures: {' | '.join(node.signatures)}")
        rendered, tokens, degraded = self._fit("\n".join(lines))
        return ViewProjection(
            kind=ViewKind.LOCAL_TEXT,
            refs=tuple(ordered),
            rendered=rendered,
            token_estimate=tokens,
            degraded=degraded,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "budget_tokens": self.budget_tokens,
            "refresh_count": self._refresh_count,
            "substantive_edit": self._edit_active,
            "current": [view.to_dict() for view in self._current],
        }
