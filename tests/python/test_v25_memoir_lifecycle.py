"""Tests for ledger #110 MEMOIR memory lifecycle."""

from __future__ import annotations

import pytest
from ultimate_coders.search.memoir_lifecycle import (
    MEMOIR_LIFECYCLE_ENV,
    MemoirError,
    MemoirLifecycle,
    MemoryItem,
    memoir_lifecycle_enabled,
)


def item(item_id: str, content: str, embedding: tuple[float, ...] = (1.0, 0.0)) -> MemoryItem:
    return MemoryItem(id=item_id, content=content, embedding=embedding)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MEMOIR_LIFECYCLE_ENV, raising=False)
    assert memoir_lifecycle_enabled() is False
    monkeypatch.setenv(MEMOIR_LIFECYCLE_ENV, "true")
    assert memoir_lifecycle_enabled() is True


def test_duplicate_memory_is_rejected() -> None:
    lifecycle = MemoirLifecycle()
    lifecycle.add(item("a", "alpha"))
    with pytest.raises(MemoirError):
        lifecycle.add(item("a", "again"))


def test_empty_embedding_is_rejected() -> None:
    with pytest.raises(MemoirError):
        MemoryItem(id="a", content="alpha", embedding=())


def test_semantic_query_prefers_aligned_memory() -> None:
    lifecycle = MemoirLifecycle()
    lifecycle.add(item("x", "x", (1.0, 0.0)))
    lifecycle.add(item("y", "y", (0.0, 1.0)))
    result = lifecycle.retrieve((1.0, 0.0), k=1)
    assert result[0].id == "x"


def test_retrieval_updates_exposures() -> None:
    lifecycle = MemoirLifecycle()
    lifecycle.add(item("a", "alpha"))
    lifecycle.retrieve((1.0, 0.0), k=1)
    assert lifecycle.items()[0].exposures == 1


def test_utility_changes_ranking() -> None:
    lifecycle = MemoirLifecycle()
    lifecycle.add(item("a", "a", (1.0, 0.0)))
    lifecycle.add(item("b", "b", (1.0, 0.0)))
    lifecycle.record_utility("b", 1.0)
    assert lifecycle.retrieve((1.0, 0.0), k=1)[0].id == "b"


def test_merge_candidates_are_similar() -> None:
    lifecycle = MemoirLifecycle(merge_threshold=0.99)
    lifecycle.add(item("a", "alpha"))
    lifecycle.add(item("b", "beta"))
    assert lifecycle.merge_candidates() == (("a", "b"),)


def test_sleep_merge_preserves_exact_content() -> None:
    lifecycle = MemoirLifecycle(merge_threshold=0.99)
    lifecycle.add(item("a", "alpha beta"))
    lifecycle.add(item("b", "alpha beta"))
    report = lifecycle.sleep_consolidate()
    assert report.merged == 1
    assert report.evidence_lost == 0
    assert report.stored == 1


def test_sleep_merge_refuses_lossy_merge() -> None:
    lifecycle = MemoirLifecycle(merge_threshold=0.5)
    lifecycle.add(item("a", "alpha"))
    lifecycle.add(item("b", "alpha beta"))
    report = lifecycle.sleep_consolidate()
    assert report.merged == 0
    assert report.evidence_lost == 1


def test_stats_and_report_are_deterministic() -> None:
    lifecycle = MemoirLifecycle()
    lifecycle.add(item("a", "alpha"))
    assert lifecycle.stats()["stored"] == 1
    report = lifecycle.sleep_consolidate()
    assert report.to_dict()["stored"] == 1
