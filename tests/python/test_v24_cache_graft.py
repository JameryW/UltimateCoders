from __future__ import annotations

import pytest
from ultimate_coders.search.cache_graft import (
    CACHE_GRAFT_ENV,
    CacheGraftPlanner,
    Document,
    LayerTable,
    cache_graft_enabled,
)


def document(doc_id: str, tokens: int) -> Document:
    return Document(doc_id, "text", tokens)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CACHE_GRAFT_ENV, raising=False)
    assert not cache_graft_enabled()
    monkeypatch.setenv(CACHE_GRAFT_ENV, "true")
    assert cache_graft_enabled()


def test_graft_respects_token_budget() -> None:
    plan = CacheGraftPlanner().graft((document("a", 100), document("b", 100)), budget_tokens=150)
    assert plan.total_graft_tokens == 150
    assert len(plan.documents) == 2
    assert plan.documents[1].truncated


def test_graft_preserves_order_and_counts() -> None:
    plan = CacheGraftPlanner().graft((document("a", 10), document("b", 20)), budget_tokens=100)
    assert [item.document_id for item in plan.documents] == ["a", "b"]
    assert plan.total_graft_tokens == 30


def test_offload_layers_reclaims_host_memory() -> None:
    plan = CacheGraftPlanner().offload_layers(
        (LayerTable(0, 80.0, 10.0), LayerTable(1, 60.0, 4.0), LayerTable(2, 38.0, 2.0))
    )
    assert plan.offloaded_gib == 178.0
    assert plan.host_memory_gib == 34.0
    assert plan.layers == (2, 1, 0)


def test_offload_deadlines_follow_layer_order() -> None:
    plan = CacheGraftPlanner().offload_layers(
        (LayerTable(0, 10.0, 20.0), LayerTable(1, 10.0, 10.0))
    )
    assert plan.deadlines == ((1, 10.0), (0, 20.0))


def test_default_plan_reports_published_graft_metrics() -> None:
    plan = CacheGraftPlanner().graft((document("a", 10),))
    assert plan.host_memory_gib == 34.0
    assert plan.decode_overhead == 0.014
    assert plan.median_throughput_gain == 0.36
    assert plan.p90_ttft_seconds == 24.0


def test_memory_reclaimed_property() -> None:
    plan = CacheGraftPlanner().graft((document("a", 10),))
    assert plan.memory_reclaimed_gib == 178.0


def test_invalid_document_is_rejected() -> None:
    with pytest.raises(ValueError):
        Document("", "text", 1)
    with pytest.raises(ValueError):
        Document("a", "text", 0)


def test_invalid_layer_is_rejected() -> None:
    with pytest.raises(ValueError):
        LayerTable(-1, 1.0, 1.0)
    with pytest.raises(ValueError):
        LayerTable(1, 0.0, 1.0)


def test_invalid_budget_is_rejected() -> None:
    with pytest.raises(ValueError):
        CacheGraftPlanner().graft((document("a", 1),), budget_tokens=0)
