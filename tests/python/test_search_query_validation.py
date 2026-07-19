"""F66 — SearchQuery.limit clamping + in_all_repos failure visibility."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from ultimate_coders.search.query import SearchQuery


def test_limit_clamps_negative_to_one() -> None:
    assert SearchQuery("q").limit(-5).to_dict()["max_results"] == 1


def test_limit_clamps_huge_to_max() -> None:
    assert SearchQuery("q").limit(999999).to_dict()["max_results"] == 1000


def test_limit_passes_valid_values() -> None:
    assert SearchQuery("q").limit(25).to_dict()["max_results"] == 25


def test_in_all_repos_failure_warns_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """list_repos failure used to `pass` silently — the search widened to all
    repos looking like a normal wide search. Now it warns."""
    engine = MagicMock()
    engine.list_repos = MagicMock(side_effect=RuntimeError("gateway down"))
    with caplog.at_level("WARNING"):
        sq = SearchQuery("q").in_all_repos(engine)
    assert sq.to_dict()["repo_ids"] == []
    assert any("widened" in r.message for r in caplog.records)
