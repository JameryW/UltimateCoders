from __future__ import annotations

import pytest
from ultimate_coders.acceptance.diff_bisect_fuzzer import (
    DIFF_BISECT_FUZZER_ENV,
    ComputationGraph,
    DifferentialBisector,
    EmbersMutator,
    FuzzCampaign,
    FuzzCase,
    GraphNode,
    diff_bisect_fuzzer_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DIFF_BISECT_FUZZER_ENV, raising=False)
    assert not diff_bisect_fuzzer_enabled()
    monkeypatch.setenv(DIFF_BISECT_FUZZER_ENV, "1")
    assert diff_bisect_fuzzer_enabled()


def test_fuzz_case_failure_detection() -> None:
    assert not FuzzCase("ok", "1", "2", "2").failed
    assert FuzzCase("bad", "1", "2", "3").failed


def test_graph_execution_order_is_topological() -> None:
    graph = ComputationGraph(
        [
            GraphNode("a", "input"),
            GraphNode("b", "add", ("a",)),
            GraphNode("c", "mul", ("b",)),
        ]
    )
    assert graph.execution_order() == ("a", "b", "c")


def test_bisect_finds_first_divergent_node() -> None:
    graph = ComputationGraph(
        [GraphNode("a", "input"), GraphNode("b", "add", ("a",)), GraphNode("c", "mul", ("b",))]
    )
    result = DifferentialBisector().bisect(graph, {"a": True, "b": True, "c": False})
    assert result.faulty_node == "c"
    assert result.divergence_index == 2
    assert result.path == ("a", "b", "c")


def test_bisect_returns_none_when_all_match() -> None:
    graph = ComputationGraph([GraphNode("a", "input")])
    result = DifferentialBisector().bisect(graph, {"a": True})
    assert result.faulty_node is None
    assert result.confidence == 0.0


def test_graph_cycle_is_rejected() -> None:
    graph = ComputationGraph([GraphNode("a", "op")])
    graph._nodes["a"] = GraphNode("a", "op", ("a",))
    with pytest.raises(ValueError):
        graph.execution_order()


def test_unknown_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        ComputationGraph([GraphNode("a", "op", ("missing",))])


def test_embers_mutator_generates_diverse_variants() -> None:
    variants = EmbersMutator().mutate("seed")
    assert len(variants) == 4
    assert len(set(variants)) == 4


def test_campaign_reports_failure_rate() -> None:
    campaign = FuzzCampaign(
        (
            FuzzCase("a", "1", "2", "2"),
            FuzzCase("b", "1", "2", "3"),
        )
    )
    assert len(campaign.failures) == 1
    assert campaign.failure_rate == 0.5


def test_campaign_triage_uses_bisector() -> None:
    graph = ComputationGraph([GraphNode("a", "input"), GraphNode("b", "op", ("a",))])
    campaign = FuzzCampaign((FuzzCase("a", "1", "2", "3"),))
    result = campaign.triage(graph, {"a": True, "b": False})
    assert result.faulty_node == "b"
