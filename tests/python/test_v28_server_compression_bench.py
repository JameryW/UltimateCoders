from __future__ import annotations

from ultimate_coders.inference.server_compression_bench import (
    CompressionBench,
    CompressionCandidate,
    ServerBench,
    ServingVariant,
    server_compression_bench_enabled,
)


def test_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv("UC_SERVER_COMPRESSION_BENCH", raising=False)
    assert server_compression_bench_enabled() is False
    monkeypatch.setenv("UC_SERVER_COMPRESSION_BENCH", "true")
    assert server_compression_bench_enabled() is True


def test_server_bench_picks_the_best_verified_variant():
    bench = ServerBench()
    report = bench.evaluate(
        (
            ServingVariant("vllm", 1.0, 100.0, True),
            ServingVariant("tuned", 4.87, 90.0, True),
            ServingVariant("slow", 10.0, 900.0, True),
        ),
        baseline_name="vllm",
        latency_budget_ms=200.0,
    )
    assert report.best.name == "tuned"
    assert report.best.value_ratio == 4.87
    assert report.accepted_count == 2


def test_server_bench_rejects_unverified_or_over_budget_variants():
    bench = ServerBench()
    report = bench.evaluate(
        (
            ServingVariant("vllm", 1.0, 100.0, True),
            ServingVariant("unverified", 5.0, 100.0, False),
            ServingVariant("late", 5.0, 300.0, True),
        ),
        baseline_name="vllm",
        latency_budget_ms=90.0,
    )
    assert report.best is None
    assert all(not score.accepted for score in report.scores)


def test_server_bench_validates_baseline_and_duplicates():
    bench = ServerBench()
    try:
        bench.evaluate(
            (ServingVariant("a", 1.0, 10.0),),
            baseline_name="missing",
            latency_budget_ms=100.0,
        )
    except ValueError as error:
        assert "baseline" in str(error)
    else:
        raise AssertionError("expected missing baseline")

    try:
        bench.evaluate(
            (ServingVariant("a", 1.0, 10.0), ServingVariant("a", 2.0, 10.0)),
            baseline_name="a",
            latency_budget_ms=100.0,
        )
    except ValueError as error:
        assert "duplicate" in str(error)
    else:
        raise AssertionError("expected duplicate variant")


def test_compression_bench_selects_lowest_ppl_and_passes():
    bench = CompressionBench()
    report = bench.evaluate(
        (
            CompressionCandidate("int8", 10.34, 0.7800, True),
            CompressionCandidate("int4", 12.0, 0.7400, True),
        ),
        baseline_perplexity=10.0,
        max_ppl_increase=0.034,
        min_accuracy=0.7733,
    )
    assert report.selected.name == "int8"
    assert report.passed is True
    assert abs(report.ppl_increase - 0.034) < 1e-9


def test_compression_bench_rejects_ppl_or_accuracy_regression():
    bench = CompressionBench()
    candidates = (
        CompressionCandidate("good", 10.2, 0.78, True),
        CompressionCandidate("bad", 10.0, 0.75, True),
    )
    report = bench.evaluate(
        candidates,
        baseline_perplexity=10.0,
        max_ppl_increase=0.034,
        min_accuracy=0.7733,
    )
    assert report.selected.name == "bad"
    assert report.passed is False


def test_local_and_end_to_end_rankings_disagree():
    bench = CompressionBench()
    candidates = (
        CompressionCandidate("a", 10.0, 0.70, True),
        CompressionCandidate("b", 10.1, 0.90, True),
        CompressionCandidate("c", 10.2, 0.80, True),
    )
    assert bench.local_ranking(candidates) == ("a", "b", "c")
    assert bench.end_to_end_ranking(candidates) == ("b", "c", "a")
    assert bench.reversals(candidates) == 2


def test_empty_compression_set_is_not_passing():
    report = CompressionBench().evaluate((), baseline_perplexity=10.0)
    assert report.selected is None
    assert report.passed is False
    assert report.reversals == 0


def test_invalid_measurements_are_rejected():
    for factory in (
        lambda: ServingVariant("", 1.0, 10.0),
        lambda: ServingVariant("a", 0.0, 10.0),
        lambda: ServingVariant("a", 1.0, 0.0),
        lambda: CompressionCandidate("", 10.0, 0.8),
        lambda: CompressionCandidate("a", 0.0, 0.8),
        lambda: CompressionCandidate("a", 10.0, 1.2),
    ):
        try:
            factory()
        except ValueError:
            pass
        else:
            raise AssertionError("expected invalid measurement")


def test_report_serialisation_is_bounded():
    report = ServerBench().evaluate(
        (ServingVariant("vllm", 1.0, 10.0, True),),
        baseline_name="vllm",
        latency_budget_ms=20.0,
    )
    payload = report.to_dict()
    assert payload["baseline"] == "vllm"
    assert payload["best"]["name"] == "vllm"
    assert payload["accepted_count"] == 1
