"""Tests for ledger item #88 (external response store and discovery database)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.response_store import (
    ArtifactKind,
    Candidate,
    DiscoveryDatabase,
    ResponseStore,
    ResponseStoreError,
    response_store_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_RESPONSE_STORE", raising=False)
    assert not response_store_enabled()
    monkeypatch.setenv("UC_RESPONSE_STORE", "yes")
    assert response_store_enabled()


def test_put_returns_compact_handle() -> None:
    store = ResponseStore()
    handle = store.put(ArtifactKind.TEST_LOG, "test output " * 100, summary="full suite")
    assert handle.artifact_id == "artifact-1"
    assert handle.tokens > 0
    assert handle.summary == "full suite"


def test_get_round_trips_content() -> None:
    store = ResponseStore()
    handle = store.put(ArtifactKind.FLAMEGRAPH, "flame graph")
    assert store.get(handle) == "flame graph"


def test_render_truncates_large_artifacts() -> None:
    store = ResponseStore()
    handle = store.put(ArtifactKind.BUILD_OUTPUT, "x" * 100)
    rendered = store.render(handle, max_chars=10)
    assert rendered.startswith("x" * 10)
    assert "truncated" in rendered


def test_query_filters_kind_and_metadata() -> None:
    store = ResponseStore()
    store.put(ArtifactKind.TEST_LOG, "log", metadata={"task": "a"})
    store.put(ArtifactKind.TEST_LOG, "log", metadata={"task": "b"})
    store.put(ArtifactKind.FLAMEGRAPH, "svg")
    assert len(store.query(kind=ArtifactKind.TEST_LOG)) == 2
    assert len(store.query(kind=ArtifactKind.TEST_LOG, metadata={"task": "a"})) == 1
    assert len(store.query(kind=ArtifactKind.FLAMEGRAPH)) == 1


def test_unknown_artifact_is_rejected() -> None:
    with pytest.raises(ResponseStoreError, match="unknown artifact"):
        ResponseStore().get("missing")


def test_discovery_best_selects_highest_score_per_cell() -> None:
    db = DiscoveryDatabase()
    db.add(Candidate("a", "cell-1", 0.4))
    db.add(Candidate("b", "cell-1", 0.8))
    db.add(Candidate("c", "cell-2", 0.2))
    assert db.best("cell-1").candidate_id == "b"
    assert db.best("missing") is None


def test_frontier_keeps_best_of_each_cell() -> None:
    db = DiscoveryDatabase()
    db.add(Candidate("a", "cell-1", 0.4))
    db.add(Candidate("b", "cell-1", 0.8))
    db.add(Candidate("c", "cell-2", 0.2))
    frontier = db.frontier()
    assert [item.candidate_id for item in frontier] == ["b", "c"]


def test_discovery_query_uses_score_and_limit() -> None:
    db = DiscoveryDatabase()
    for index in range(5):
        db.add(Candidate(f"c{index}", "cell-1", index / 10))
    assert len(db.query(min_score=0.3, limit=2)) == 2
    assert len(db.query(cell="cell-1", min_score=0.0)) == 5


def test_duplicate_candidate_is_rejected() -> None:
    db = DiscoveryDatabase()
    candidate = Candidate("a", "cell", 0.5)
    db.add(candidate)
    with pytest.raises(ResponseStoreError, match="duplicate"):
        db.add(Candidate("a", "other", 0.7))
