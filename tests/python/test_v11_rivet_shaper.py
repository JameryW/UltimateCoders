"""Tests for the v11 Rivet context shaper (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.rivet_shaper import (
    UC_RIVET_SHAPER,
    EvidenceItem,
    Representation,
    RivetShaper,
    extract_identifiers,
    rivet_shaper_enabled,
    shape_context,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_RIVET_SHAPER, raising=False)
    assert rivet_shaper_enabled() is False
    monkeypatch.setenv(UC_RIVET_SHAPER, "on")
    assert rivet_shaper_enabled() is True


def test_extracts_camel_and_snake_identifiers() -> None:
    identifiers = extract_identifiers("Fix parse_query in FooBarService")
    assert "parse_query" in identifiers
    assert "FooBarService" in identifiers
    assert "Foo" in identifiers


def test_signal_grounding_prioritises_exact_symbol() -> None:
    pool = [
        EvidenceItem(
            path="src/a.py",
            symbol="parse_query",
            signature="def parse_query(q)",
            content="",
        ),
        EvidenceItem(
            path="src/b.py",
            symbol="unrelated",
            signature="",
            content="parse_query mention",
        ),
    ]
    ranked = RivetShaper().signal_grounding("parse_query returns wrong rows", pool)
    assert ranked[0].symbol == "parse_query"
    assert ranked[0].score > ranked[1].score


def test_utility_linking_follows_dependencies() -> None:
    seed = EvidenceItem(
        path="src/service.py",
        symbol="Service",
        content="class Service: ...",
        dependencies=("src/cache.py",),
        score=10.0,
    )
    dependency = EvidenceItem(path="src/cache.py", symbol="Cache", content="class Cache: ...")
    shaper = RivetShaper()
    linked = shaper.utility_linking([seed], [seed, dependency])
    assert {item.path for item in linked} == {"src/service.py", "src/cache.py"}


def test_budget_shaping_degrades_instead_of_dropping() -> None:
    item = EvidenceItem(
        path="src/long.py",
        symbol="long_function",
        signature="def long_function(value: int) -> str",
        content="x" * 400,
    )
    shaped = RivetShaper().budget_shaping([item], budget_tokens=12)
    assert len(shaped) == 1
    assert shaped[0].representation in (Representation.SIGNATURE, Representation.SUMMARY)


def test_shape_context_reports_counts() -> None:
    pool = [
        EvidenceItem(path="src/a.py", symbol="parse_query", content="def parse_query(q): return q"),
        EvidenceItem(path="src/b.py", symbol="helper", content="def helper(): pass"),
    ]
    result = shape_context("parse_query fails", pool, budget_tokens=100)
    assert result.candidate_count == 2
    assert result.expanded_count >= 1
    assert result.total_tokens <= 100
    assert "parse_query" in result.render()
