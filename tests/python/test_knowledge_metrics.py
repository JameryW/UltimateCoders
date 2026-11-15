"""Tests for knowledge-layer A-B metrics (knowledge/metrics.py)."""

from __future__ import annotations

import json
from pathlib import Path

from ultimate_coders.knowledge.attempt_chain import AttemptChainLedger, AttemptRound
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.metrics import (
    MAX_SIGNATURES,
    AttemptSample,
    collect_samples,
    compare_metrics,
    compute_metrics,
    main,
    signature_histogram,
)
from ultimate_coders.knowledge.model import AttemptRef, EvidenceKind
from ultimate_coders.knowledge.rollout import KnowledgeStage
from ultimate_coders.runtime_state import RuntimeState


def _state(tmp_path: Path, name: str = "state.db") -> RuntimeState:
    return RuntimeState(tmp_path / name, database_url="")


def _record_attempt(
    ledgers: tuple[AttemptChainLedger, EvidenceLedger],
    *,
    task: str,
    subtask: str,
    attempt: int,
    success: bool,
    signature: str = "",
    tokens: tuple[int, int] | None = None,
) -> None:
    chain, evidence = ledgers
    ref = AttemptRef(
        task_id=task,
        subtask_id=subtask,
        dispatch_attempt=0,
        attempt=attempt,
        worker_id="w-test",
        graph_id=task,
    )
    chain.record(
        AttemptRound(
            attempt=ref,
            outcome="completed" if success else "failed",
            failure="" if success else signature,
        )
    )
    payload: dict[str, object] = {"success": success, "attempt": attempt}
    if tokens is not None:
        payload["usage"] = {"input_tokens": tokens[0], "output_tokens": tokens[1]}
    evidence.record_evidence(
        attempt=ref,
        kind=EvidenceKind.ATTEMPT_OUTCOME,
        payload=payload,
        summary="attempt outcome",
    )
    if not success and signature:
        evidence.record_evidence(
            attempt=ref,
            kind=EvidenceKind.FAILURE_SIGNATURE,
            payload={"signature": signature},
            summary="attempt failed",
        )


def _ledgers(tmp_path: Path, name: str = "state.db") -> tuple[AttemptChainLedger, EvidenceLedger]:
    state = _state(tmp_path, name)
    return (
        AttemptChainLedger(state),
        EvidenceLedger(state, stage=KnowledgeStage.SHADOW),
    )


def test_metrics_aggregate_attempts_success_and_repeated_signatures(tmp_path: Path) -> None:
    ledgers = _ledgers(tmp_path)
    _record_attempt(
        ledgers,
        task="t1",
        subtask="s1",
        attempt=0,
        success=False,
        signature="boom",
        tokens=(100, 20),
    )
    _record_attempt(
        ledgers,
        task="t1",
        subtask="s1",
        attempt=1,
        success=False,
        signature="boom",
        tokens=(50, 10),
    )
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=2, success=True, tokens=(30, 5))
    _record_attempt(ledgers, task="t1", subtask="s2", attempt=0, success=True, tokens=(10, 5))
    _record_attempt(ledgers, task="t2", subtask="s3", attempt=0, success=False, signature="other")

    samples = collect_samples(_state(tmp_path))
    metrics = compute_metrics(samples)
    payload = metrics.to_dict()
    assert payload["tasks"] == 2
    assert payload["subtasks"] == 3
    assert payload["attempts"] == 5
    assert payload["failures"] == 3
    assert payload["repeated_failures"] == 1
    assert payload["succeeded_subtasks"] == 2
    assert payload["success_rate"] == 0.6667
    assert payload["avg_attempts"] == 1.6667
    assert payload["repeated_failure_rate"] == 0.3333
    assert payload["tokens_reported"] == 230
    assert payload["attempts_with_usage"] == 4
    assert payload["tokens_per_subtask"] == 76.67


def test_usage_absence_stays_unknown(tmp_path: Path) -> None:
    ledgers = _ledgers(tmp_path)
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=0, success=False, signature="boom")
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=1, success=True)

    metrics = compute_metrics(collect_samples(_state(tmp_path)))
    assert metrics.tokens_per_attempt is None
    assert metrics.tokens_per_subtask is None
    assert metrics.to_dict()["tokens_reported"] == 0


def test_empty_state_yields_empty_metrics(tmp_path: Path) -> None:
    metrics = compute_metrics(collect_samples(_state(tmp_path)))
    assert metrics.attempts == 0
    assert metrics.success_rate == 0.0
    assert metrics.repeated_failure_rate == 0.0


def _sample(
    task: str,
    subtask: str,
    attempt: int,
    success: bool,
    signature: str = "",
    tokens: int = 0,
) -> AttemptSample:
    return AttemptSample(
        task_id=task,
        subtask_id=subtask,
        dispatch_attempt=0,
        attempt=attempt,
        success=success,
        signature=signature,
        input_tokens=tokens,
        output_tokens=0,
    )


def test_compare_reports_improved_for_fewer_attempts_and_repeats() -> None:
    baseline = [
        _sample("t1", "s1", 0, False, "boom", 100),
        _sample("t1", "s1", 1, False, "boom", 100),
        _sample("t1", "s1", 2, True, "", 100),
        _sample("t2", "s2", 0, False, "zap", 100),
        _sample("t2", "s2", 1, True, "", 100),
    ]
    variant = [
        _sample("t1", "s1", 0, False, "zap", 100),
        _sample("t1", "s1", 1, True, "", 100),
        _sample("t2", "s2", 0, True, "", 100),
    ]
    report = compare_metrics(baseline, variant)
    assert report.verdict == "improved"
    assert "attempts_reduced" in report.reasons
    assert report.paired_tasks == 2


def test_compare_reports_regressed_when_success_drops() -> None:
    baseline = [_sample("t1", "s1", 0, True), _sample("t2", "s2", 0, True)]
    variant = [
        _sample("t1", "s1", 0, True),
        _sample("t2", "s2", 0, False, "boom"),
        _sample("t2", "s2", 1, False, "boom"),
    ]
    report = compare_metrics(baseline, variant)
    assert report.verdict == "regressed"
    assert report.reasons == ("success_rate_lower",)


def test_compare_requires_paired_tasks() -> None:
    baseline = [_sample("t1", "s1", 0, True)]
    variant = [_sample("t9", "s9", 0, True)]
    report = compare_metrics(baseline, variant)
    assert report.verdict == "insufficient_data"
    assert report.paired_tasks == 0


def test_compare_flags_token_regression_as_inconclusive() -> None:
    baseline = [
        _sample("t1", "s1", 0, False, "boom", 100),
        _sample("t1", "s1", 1, True, "", 100),
        _sample("t2", "s2", 0, False, "boom2", 100),
        _sample("t2", "s2", 1, True, "", 100),
    ]
    variant = [
        _sample("t1", "s1", 0, False, "zap", 10_000),
        _sample("t1", "s1", 1, True, "", 10_000),
        _sample("t2", "s2", 0, False, "zap2", 10_000),
        _sample("t2", "s2", 1, True, "", 10_000),
    ]
    report = compare_metrics(baseline, variant)
    assert report.verdict == "inconclusive"
    assert "tokens_worse" in report.reasons


def test_cli_single_state_emits_json(tmp_path: Path, capsys) -> None:
    ledgers = _ledgers(tmp_path)
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=0, success=True, tokens=(3, 4))
    code = main(["--state", str(tmp_path / "state.db"), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["attempts"] == 1
    assert payload["success_rate"] == 1.0


def test_cli_compares_two_states(tmp_path: Path, capsys) -> None:
    baseline = _ledgers(tmp_path, "baseline.db")
    _record_attempt(baseline, task="t1", subtask="s1", attempt=0, success=False, signature="boom")
    _record_attempt(baseline, task="t1", subtask="s1", attempt=1, success=False, signature="boom")
    _record_attempt(baseline, task="t1", subtask="s1", attempt=2, success=True)
    variant = _ledgers(tmp_path, "variant.db")
    _record_attempt(variant, task="t1", subtask="s1", attempt=0, success=True)

    code = main(
        [
            "--baseline",
            str(tmp_path / "baseline.db"),
            "--variant",
            str(tmp_path / "variant.db"),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["paired_tasks"] == 1
    assert payload["verdict"] == "insufficient_data"
    assert payload["baseline"]["attempts"] == 3
    assert payload["variant"]["attempts"] == 1


def test_signature_histogram_counts_only_failed_reported_signatures() -> None:
    samples = [
        _sample("t1", "s1", 0, False, "boom"),
        _sample("t1", "s1", 1, False, "boom"),
        _sample("t1", "s1", 2, True),
        _sample("t1", "s1", 3, False),
        _sample("t2", "s2", 0, False, "zap"),
        _sample("t2", "s2", 1, False, "zap"),
        _sample("t2", "s2", 2, False, "zap"),
    ]
    rows = signature_histogram(samples)
    assert rows == [
        {"signature": "zap", "count": 3},
        {"signature": "boom", "count": 2},
    ]


def test_signature_histogram_bounds_and_sorts_by_count_then_name() -> None:
    samples = [_sample("t1", "s1", index, False, "same") for index in range(3)]
    samples += [_sample("t1", "s1", 3 + index, False, "alpha") for index in range(2)]
    samples += [_sample("t1", "s1", 9, False, "beta")]
    rows = signature_histogram(samples, limit=2)
    assert [row["signature"] for row in rows] == ["same", "alpha"]
    assert signature_histogram(samples, limit=0) == []
    assert len(signature_histogram(samples)) <= MAX_SIGNATURES
    assert signature_histogram(samples) == signature_histogram(samples)


def test_cli_signatures_flag_adds_histogram_to_json(tmp_path: Path, capsys) -> None:
    ledgers = _ledgers(tmp_path)
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=0, success=False, signature="boom")
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=1, success=False, signature="boom")
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=2, success=True)
    code = main(["--state", str(tmp_path / "state.db"), "--json", "--signatures"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["signatures"] == [{"signature": "boom", "count": 2}]


def test_cli_without_json_prints_metric_table_not_json(tmp_path: Path, capsys) -> None:
    ledgers = _ledgers(tmp_path)
    _record_attempt(ledgers, task="t1", subtask="s1", attempt=0, success=True)
    code = main(["--state", str(tmp_path / "state.db")])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("metric value")
    assert "attempts 1" in out
    assert not out.lstrip().startswith("{")


def test_cli_paired_comparison_can_include_both_histograms(tmp_path: Path, capsys) -> None:
    baseline = _ledgers(tmp_path, "baseline.db")
    _record_attempt(baseline, task="t1", subtask="s1", attempt=0, success=False, signature="boom")
    _record_attempt(baseline, task="t1", subtask="s1", attempt=1, success=True)
    variant = _ledgers(tmp_path, "variant.db")
    _record_attempt(variant, task="t1", subtask="s1", attempt=0, success=False, signature="zap")
    _record_attempt(variant, task="t1", subtask="s1", attempt=1, success=True)
    code = main(
        [
            "--baseline",
            str(tmp_path / "baseline.db"),
            "--variant",
            str(tmp_path / "variant.db"),
            "--json",
            "--signatures",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["signatures"]["baseline"] == [{"signature": "boom", "count": 1}]
    assert payload["signatures"]["variant"] == [{"signature": "zap", "count": 1}]
