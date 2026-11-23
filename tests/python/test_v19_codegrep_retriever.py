"""Tests for the V19 CodeGrep precision-gated retriever (#75)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.codegrep_retriever import (
    CODEGREP_RETRIEVER_ENV,
    CodeGrepRetriever,
    CodeIndex,
    RetrievalAction,
    codegrep_retriever_enabled,
)


def build_index() -> CodeIndex:
    index = CodeIndex()
    index.add_file(
        "src/cache.py",
        {
            "Cache": "class Cache:\n    def invalidate(self): ...",
            "invalidate": "def invalidate(self): ...",
        },
    )
    index.add_file(
        "src/api.py",
        {"API": "class API:\n    def get(self): ..."},
    )
    index.add_file(
        "tests/test_cache.py",
        {"test_cache": "def test_cache(): ..."},
    )
    return index


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CODEGREP_RETRIEVER_ENV, raising=False)
    assert not codegrep_retriever_enabled()
    monkeypatch.setenv(CODEGREP_RETRIEVER_ENV, "true")
    assert codegrep_retriever_enabled()


def test_index_grep_glob_and_read() -> None:
    index = build_index()
    assert {hit.symbol for hit in index.grep("cache")} == {"Cache", "test_cache"}
    assert {hit.path for hit in index.glob("*.py")} == {
        "src/api.py",
        "src/cache.py",
        "tests/test_cache.py",
    }
    assert {hit.symbol for hit in index.read("src/cache.py")} == {
        "Cache",
        "invalidate",
    }


def test_retrieve_returns_high_precision_hits() -> None:
    report = CodeGrepRetriever().retrieve(
        "cache",
        build_index(),
        relevant_paths=("src/cache.py", "tests/test_cache.py"),
    )
    assert report.hits
    assert report.precision == 1.0
    assert report.fallback is False
    assert report.repo_map == ""
    assert report.rounds[0].action is RetrievalAction.GREP


def test_low_precision_triggers_repo_map_fallback() -> None:
    report = CodeGrepRetriever(precision_threshold=0.8).retrieve(
        "cache",
        build_index(),
        relevant_paths=("src/unrelated.py",),
    )
    assert report.fallback is True
    assert report.precision == 0.0
    assert "src/cache.py" in report.repo_map
    assert len(report.repo_map) <= 8000 * 4


def test_grep_miss_falls_back_to_glob_and_read() -> None:
    report = CodeGrepRetriever().retrieve("*.py", build_index())
    actions = [round_.action for round_ in report.rounds]
    assert RetrievalAction.GLOB in actions
    assert RetrievalAction.READ in actions
    assert report.hits


def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="query must not be empty"):
        CodeGrepRetriever().retrieve(" ", build_index())


def test_report_to_dict_is_bounded_and_serializable() -> None:
    report = CodeGrepRetriever().retrieve("cache", build_index())
    payload = report.to_dict()
    assert payload["precision"] == 1.0
    assert payload["fallback"] is False
    assert isinstance(payload["rounds"], list)
    assert payload["repo_map_chars"] == 0


def test_invalid_precision_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="precision_threshold"):
        CodeGrepRetriever(precision_threshold=1.5)
