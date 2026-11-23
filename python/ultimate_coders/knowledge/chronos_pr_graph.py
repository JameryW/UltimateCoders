"""Chronos: PR-evolution experience graph and patch stewardship (#30).

Ledger item #30 adapts Chronos (arXiv:2610.11578).  Merged pull requests are
distilled into four-field experience cards — Summary, Signals, Motivation,
Solution — and linked into a directed evolution graph with eleven relation
types.  Retrieval starts from K=5 entry cards and expands H=3 hops (at most
ten cards).  For complex nodes, a ChangeAgent and a ValidationStrategyAgent
independently propose patches, and the EvolutionSteward selects exactly one of
those two verified candidates, grounded in the historical Motivation/Solution
cards.  It never invents a third patch.

Feature flag: ``UC_CHRONOS_PR_GRAPH`` (default off).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "UC_CHRONOS_PR_GRAPH",
    "EvolutionEdgeKind",
    "PRCard",
    "EvolutionEdge",
    "ChronosPRGraph",
    "PatchCandidate",
    "StewardDecision",
    "EvolutionSteward",
    "chronos_pr_graph_enabled",
]

UC_CHRONOS_PR_GRAPH = "UC_CHRONOS_PR_GRAPH"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def chronos_pr_graph_enabled() -> bool:
    """Return whether the Chronos PR evolution graph is enabled."""

    return os.environ.get(UC_CHRONOS_PR_GRAPH, "").strip().lower() in _TRUTHY


class EvolutionEdgeKind(str, Enum):
    FILE_OVERLAP = "file_overlap"
    INTRODUCED_THEN_MODIFIED = "introduced_then_modified"
    REFERENCE = "reference"
    BACKPORT = "backport"
    SAME_AUTHOR_FOLLOWUP = "same_author_followup"
    SHARED_REVIEWER = "shared_reviewer"
    SAME_MILESTONE = "same_milestone"
    REVERT = "revert"
    SUPERSEDES = "supersedes"
    DEPENDS_ON = "depends_on"
    RELATED_CHANGE = "related_change"


@dataclass(frozen=True)
class PRCard:
    pr_id: str
    summary: str
    signals: tuple[str, ...] = ()
    motivation: str = ""
    solution: str = ""
    files: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()
    milestone: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not str(self.pr_id).strip():
            raise ValueError("pr_id must be non-empty")
        object.__setattr__(self, "pr_id", str(self.pr_id).strip())
        object.__setattr__(self, "summary", str(self.summary).strip())
        object.__setattr__(self, "motivation", str(self.motivation).strip())
        object.__setattr__(self, "solution", str(self.solution).strip())
        for name in ("signals", "files", "authors", "reviewers"):
            object.__setattr__(
                self,
                name,
                tuple(str(value).strip() for value in getattr(self, name) if str(value).strip()),
            )
        object.__setattr__(self, "milestone", str(self.milestone).strip())
        object.__setattr__(self, "timestamp", str(self.timestamp).strip())

    def searchable(self) -> str:
        return " ".join(
            (
                self.summary,
                " ".join(self.signals),
                self.motivation,
                self.solution,
                " ".join(self.files),
            )
        ).lower()

    def to_dict(self) -> dict[str, object]:
        return {
            "pr_id": self.pr_id,
            "summary": self.summary,
            "signals": list(self.signals),
            "motivation": self.motivation,
            "solution": self.solution,
            "files": list(self.files),
            "authors": list(self.authors),
            "reviewers": list(self.reviewers),
            "milestone": self.milestone,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class EvolutionEdge:
    source: str
    target: str
    kind: EvolutionEdgeKind
    weight: float = 1.0
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", str(self.source).strip())
        object.__setattr__(self, "target", str(self.target).strip())
        if isinstance(self.kind, str):
            object.__setattr__(self, "kind", EvolutionEdgeKind(self.kind))
        weight = float(self.weight)
        if not 1.0 <= weight <= 1.5:
            raise ValueError("evolution edge weight must be in [1.0, 1.5]")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "reason", str(self.reason).strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind.value,
            "weight": round(self.weight, 6),
            "reason": self.reason,
        }


def _terms(text: str) -> set[str]:
    return {
        token.strip(".,:;()[]{}'\"`").lower()
        for token in text.split()
        if len(token.strip(".,:;()[]{}'\"`")) >= 2
    }


@dataclass
class ChronosPRGraph:
    """Directed experience graph over merged pull-request cards."""

    cards: dict[str, PRCard] = field(default_factory=dict)
    edges: list[EvolutionEdge] = field(default_factory=list)

    def add_card(self, card: PRCard) -> None:
        self.cards[card.pr_id] = card

    def add_edge(
        self,
        source: str,
        target: str,
        kind: EvolutionEdgeKind | str,
        *,
        weight: float = 1.0,
        reason: str = "",
    ) -> EvolutionEdge:
        if source not in self.cards or target not in self.cards:
            raise KeyError("both edge endpoints must exist")
        edge = EvolutionEdge(source=source, target=target, kind=kind, weight=weight, reason=reason)
        self.edges.append(edge)
        return edge

    def auto_link(self) -> int:
        """Infer same-repository evolution edges between all card pairs."""

        added = 0
        ids = sorted(self.cards)
        for index, left_id in enumerate(ids):
            left = self.cards[left_id]
            for right_id in ids[index + 1 :]:
                right = self.cards[right_id]
                shared_files = set(left.files) & set(right.files)
                if shared_files:
                    self.edges.append(
                        EvolutionEdge(
                            source=left_id,
                            target=right_id,
                            kind=EvolutionEdgeKind.FILE_OVERLAP,
                            weight=min(1.5, 1.0 + 0.1 * len(shared_files)),
                            reason="shared files: " + ", ".join(sorted(shared_files)),
                        )
                    )
                    added += 1
                if set(left.authors) & set(right.authors):
                    self.edges.append(
                        EvolutionEdge(
                            source=left_id,
                            target=right_id,
                            kind=EvolutionEdgeKind.SAME_AUTHOR_FOLLOWUP,
                            weight=1.1,
                        )
                    )
                    added += 1
                if set(left.reviewers) & set(right.reviewers):
                    self.edges.append(
                        EvolutionEdge(
                            source=left_id,
                            target=right_id,
                            kind=EvolutionEdgeKind.SHARED_REVIEWER,
                            weight=1.05,
                        )
                    )
                    added += 1
                if left.milestone and left.milestone == right.milestone:
                    self.edges.append(
                        EvolutionEdge(
                            source=left_id,
                            target=right_id,
                            kind=EvolutionEdgeKind.SAME_MILESTONE,
                            weight=1.0,
                        )
                    )
                    added += 1
        return added

    def search(self, query: str, k: int = 5) -> tuple[PRCard, ...]:
        if k < 1:
            raise ValueError("k must be >= 1")
        query_terms = _terms(query or "")
        scored: list[tuple[float, str, PRCard]] = []
        for pr_id, card in self.cards.items():
            terms = _terms(card.searchable())
            overlap = len(query_terms & terms)
            if overlap:
                scored.append((float(overlap), pr_id, card))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(card for _, _, card in scored[:k])

    def expand(
        self, entries: Sequence[PRCard], hops: int = 3, max_cards: int = 10
    ) -> tuple[PRCard, ...]:
        if hops < 0:
            raise ValueError("hops must be >= 0")
        if max_cards < 1:
            raise ValueError("max_cards must be >= 1")
        selected = {card.pr_id: card for card in entries}
        frontier = list(entries)
        for _ in range(hops):
            candidates: list[tuple[float, str]] = []
            for card in frontier:
                for edge in self.edges:
                    if edge.source == card.pr_id:
                        neighbour = edge.target
                    elif edge.target == card.pr_id:
                        neighbour = edge.source
                    else:
                        continue
                    if neighbour in selected or neighbour not in self.cards:
                        continue
                    candidates.append((edge.weight, neighbour))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            frontier = []
            for _, neighbour in candidates:
                if neighbour in selected:
                    continue
                selected[neighbour] = self.cards[neighbour]
                frontier.append(self.cards[neighbour])
                if len(selected) >= max_cards:
                    return tuple(selected.values())
        return tuple(list(selected.values())[:max_cards])

    def retrieve(
        self, task: str, *, k: int = 5, hops: int = 3, max_cards: int = 10
    ) -> tuple[PRCard, ...]:
        entries = self.search(task, k=k)
        return self.expand(entries, hops=hops, max_cards=max_cards)

    def context_block(self, task: str, *, k: int = 5, hops: int = 3, max_cards: int = 10) -> str:
        cards = self.retrieve(task, k=k, hops=hops, max_cards=max_cards)
        blocks: list[str] = []
        for card in cards:
            blocks.append(
                "\n".join(
                    (
                        f"PR {card.pr_id}: {card.summary}",
                        f"Signals: {', '.join(card.signals) or '-'}",
                        f"Motivation: {card.motivation or '-'}",
                        f"Solution: {card.solution or '-'}",
                    )
                )
            )
        return "\n\n".join(blocks)


@dataclass(frozen=True)
class PatchCandidate:
    candidate_id: str
    agent: str
    patch: str
    verified: bool
    risk: float = 0.0
    touched_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise ValueError("candidate_id must be non-empty")
        object.__setattr__(self, "candidate_id", str(self.candidate_id).strip())
        object.__setattr__(self, "agent", str(self.agent).strip())
        object.__setattr__(self, "patch", str(self.patch))
        object.__setattr__(self, "verified", bool(self.verified))
        object.__setattr__(self, "risk", float(self.risk))
        object.__setattr__(
            self,
            "touched_files",
            tuple(str(value).strip() for value in self.touched_files if str(value).strip()),
        )


@dataclass(frozen=True)
class StewardDecision:
    selected: PatchCandidate
    rejected: PatchCandidate | None
    rationale: str
    historical_support: float

    def to_dict(self) -> dict[str, object]:
        return {
            "selected": self.selected.candidate_id,
            "rejected": self.rejected.candidate_id if self.rejected else None,
            "rationale": self.rationale,
            "historical_support": round(self.historical_support, 6),
        }


class EvolutionSteward:
    """Select exactly one of two verified candidate patches."""

    def select(
        self,
        change_candidate: PatchCandidate | None,
        validation_candidate: PatchCandidate | None,
        *,
        history: Sequence[PRCard] = (),
    ) -> StewardDecision:
        candidates = [
            candidate
            for candidate in (change_candidate, validation_candidate)
            if candidate is not None
        ]
        if not candidates:
            raise ValueError("steward needs at least one candidate patch")
        if any(not candidate.verified for candidate in candidates):
            raise ValueError("steward cannot select an unverified candidate")
        if len(candidates) == 1:
            candidate = candidates[0]
            return StewardDecision(
                selected=candidate,
                rejected=None,
                rationale="only one verified candidate was submitted",
                historical_support=self._historical_support(candidate, history),
            )
        if candidates[0].patch == candidates[1].patch:
            candidate = candidates[0]
            return StewardDecision(
                selected=candidate,
                rejected=candidates[1],
                rationale="both agents produced the same patch",
                historical_support=self._historical_support(candidate, history),
            )

        def rank(candidate: PatchCandidate) -> tuple[float, str]:
            support = self._historical_support(candidate, history)
            # Lower risk and stronger historical support win; ties are stable.
            return (-(support - candidate.risk), candidate.candidate_id)

        ordered = sorted(candidates, key=rank)
        selected, rejected = ordered[0], ordered[1]
        return StewardDecision(
            selected=selected,
            rejected=rejected,
            rationale=(
                "selected verified candidate with better historical support and risk"
            ),
            historical_support=self._historical_support(selected, history),
        )

    @staticmethod
    def _historical_support(candidate: PatchCandidate, history: Sequence[PRCard]) -> float:
        patch_terms = _terms(candidate.patch)
        score = 0.0
        for card in history:
            card_terms = _terms(" ".join((card.summary, card.motivation, card.solution)))
            if not card_terms:
                continue
            score += len(patch_terms & card_terms) / max(len(card_terms), 1)
        return score
