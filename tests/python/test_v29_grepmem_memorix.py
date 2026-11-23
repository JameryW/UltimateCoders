"""Tests for the GrepMem/Memorix memory path (#133)."""

from __future__ import annotations

import pytest
from ultimate_coders.memory.grepmem_memorix import (
    GREPMEM_MEMORIX_ENV,
    EvidenceLossReport,
    GrepMemMemorix,
    GrepMemStore,
    RefusalCompressionGuard,
    VersionedView,
    VersionedViewStore,
    detect_evidence_loss,
    grepmem_memorix_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GREPMEM_MEMORIX_ENV, raising=False)
    assert not grepmem_memorix_enabled()
    monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "true")
    assert grepmem_memorix_enabled()


def test_append_only_log_queries_with_regex() -> None:
    store = GrepMemStore()
    first = store.append("cache invalidation failed in src/cache.py")
    second = store.append("build succeeded")
    assert first.seq == 0
    assert second.seq == 1
    assert len(store) == 2
    assert [entry.text for entry in store.query("cache|build")] == [first.text, second.text]
    assert [entry.text for entry in store.query("cache", limit=1)] == [first.text]


def test_log_rejects_bad_patterns_and_empty_limits() -> None:
    store = GrepMemStore()
    store.append("hello")
    with pytest.raises(ValueError, match="invalid regular expression"):
        store.query("[")
    with pytest.raises(ValueError, match="limit must be positive"):
        store.query("hello", limit=0)


def test_log_capacity_and_entry_validation() -> None:
    store = GrepMemStore(max_entries=1)
    store.append("one")
    with pytest.raises(ValueError, match="capacity"):
        store.append("two")
    fresh = GrepMemStore()
    with pytest.raises(ValueError, match="text must not be empty"):
        fresh.append(" ")
    with pytest.raises(ValueError, match="source must not be empty"):
        fresh.append("ok", source=" ")


def test_versioned_views_are_pinned_and_rebuild_once() -> None:
    views = VersionedViewStore()
    views.put(VersionedView("abc", "tree", "src/"))
    views.put(VersionedView("abc", "tree", "src/ app/"))
    views.put(VersionedView("abc", "symbols", "Cache"))
    views.put(VersionedView("def", "tree", "lib/"))
    assert views.build_count() == 3
    assert views.get("abc", "tree") is not None
    assert views.get("abc", "tree").content == "src/ app/"
    assert [view.name for view in views.list_for_commit("abc")] == ["symbols", "tree"]


def test_versioned_views_validate_fields_and_capacity() -> None:
    views = VersionedViewStore(max_views=1)
    views.put(VersionedView("abc", "tree", "src/"))
    with pytest.raises(ValueError, match="capacity"):
        views.put(VersionedView("abc", "symbols", "Cache"))
    with pytest.raises(ValueError, match="commit must not be empty"):
        VersionedView(" ", "tree", "src/")
    with pytest.raises(ValueError, match="content must not be empty"):
        VersionedView("abc", "tree", " ")


def test_evidence_loss_reports_only_terms_present_in_source() -> None:
    report = detect_evidence_loss(
        "cache mismatch and snapshot eviction",
        "cache mismatch",
        ("cache", "snapshot", "absent"),
    )
    assert report == EvidenceLossReport(("cache", "snapshot", "absent"), ("snapshot",), True)
    assert report.to_dict()["silent_loss"] is True


def test_refusal_guard_escalates_only_with_raw_evidence() -> None:
    guard = RefusalCompressionGuard()
    assert guard.should_escalate("I don't know where it is", "raw facts")
    assert not guard.should_escalate("I don't know where it is", " ")
    assert not guard.should_escalate("the cache is invalidated", "raw facts")


def test_facade_composes_log_views_and_audit() -> None:
    memory = GrepMemMemorix()
    memory.remember("Tried cache invalidation", source="agent")
    memory.pin_view("abc", "tree", "src/")
    assert memory.recall("cache")[0].source == "agent"
    assert memory.view_for("abc", "tree").content == "src/"
    audit = memory.audit("cache", "cache invalidated", ("cache", "invalidated"))
    assert audit.missing == ("invalidated",)
