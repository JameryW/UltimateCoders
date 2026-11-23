"""Tests for the v11 Chronos PR evolution graph (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.knowledge.chronos_pr_graph import (
    UC_CHRONOS_PR_GRAPH,
    ChronosPRGraph,
    EvolutionEdgeKind,
    EvolutionSteward,
    PatchCandidate,
    PRCard,
    chronos_pr_graph_enabled,
)


def _card(pr_id: str, summary: str, files: tuple[str, ...]) -> PRCard:
    return PRCard(
        pr_id=pr_id,
        summary=summary,
        signals=("bug",),
        motivation="fix regression",
        solution="guard the cache",
        files=files,
        authors=("alice",),
        reviewers=("bob",),
        milestone="v1",
    )


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_CHRONOS_PR_GRAPH, raising=False)
    assert chronos_pr_graph_enabled() is False
    monkeypatch.setenv(UC_CHRONOS_PR_GRAPH, "1")
    assert chronos_pr_graph_enabled() is True


def test_auto_link_builds_evolution_edges() -> None:
    graph = ChronosPRGraph()
    graph.add_card(_card("10", "cache regression fix", ("src/cache.py",)))
    graph.add_card(_card("11", "cache follow-up", ("src/cache.py",)))
    assert graph.auto_link() >= 2
    kinds = {edge.kind for edge in graph.edges}
    assert EvolutionEdgeKind.FILE_OVERLAP in kinds
    assert EvolutionEdgeKind.SAME_AUTHOR_FOLLOWUP in kinds


def test_search_and_expand() -> None:
    graph = ChronosPRGraph()
    graph.add_card(_card("10", "cache regression fix", ("src/cache.py",)))
    graph.add_card(_card("11", "cache follow-up", ("src/cache.py",)))
    graph.add_card(_card("12", "unrelated docs", ("docs/readme.md",)))
    graph.auto_link()
    entries = graph.search("cache", k=1)
    assert entries[0].pr_id == "10"
    expanded = graph.expand(entries, hops=1, max_cards=10)
    assert {card.pr_id for card in expanded} >= {"10", "11"}


def test_context_block_has_four_fields() -> None:
    graph = ChronosPRGraph()
    graph.add_card(_card("10", "cache regression fix", ("src/cache.py",)))
    block = graph.context_block("cache")
    assert "PR 10" in block
    assert "Signals:" in block
    assert "Motivation:" in block
    assert "Solution:" in block


def test_steward_selects_one_verified_candidate() -> None:
    history = [_card("10", "cache regression fix", ("src/cache.py",))]
    change = PatchCandidate("change", "change-agent", "guard the cache", True, risk=0.2)
    validation = PatchCandidate(
        "validation", "validation-agent", "unrelated rewrite", True, risk=0.4
    )
    decision = EvolutionSteward().select(change, validation, history=history)
    assert decision.selected.candidate_id == "change"
    assert decision.rejected is not None
    assert decision.rejected.candidate_id == "validation"


def test_steward_rejects_unverified_candidate() -> None:
    with pytest.raises(ValueError, match="unverified"):
        EvolutionSteward().select(
            PatchCandidate("a", "change-agent", "patch", False),
            None,
        )
