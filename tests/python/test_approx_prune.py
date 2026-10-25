"""Approx prune-only path and auto-disable (ticket 08 / D4)."""

from __future__ import annotations

import json
import math

import pytest
from ultimate_coders.inference import (
    APPROX_EVAL_ARMED_ENV,
    APPROX_EVAL_ENFORCE_ENV,
    DEFAULT_MAX_DISCARD_FRACTION,
    PruneDecision,
    PruneResult,
    approx_eval_armed,
    approx_eval_ready_to_prune,
    disarm_approx_eval,
    filter_ranked_library,
    prune_candidates,
    published_uplift_verdict,
    refuse_approx_sourced_uplift,
    retest_agreement_on_change,
)
from ultimate_coders.inference.models import REPORT_UPLIFT_ENFORCE_ENV


def _arm(monkeypatch) -> None:
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, "1")
    monkeypatch.setenv(APPROX_EVAL_ARMED_ENV, "1")


def test_default_max_discard_fraction():
    assert DEFAULT_MAX_DISCARD_FRACTION == 0.5


def test_unarmed_prune_keeps_all(monkeypatch):
    monkeypatch.delenv(APPROX_EVAL_ENFORCE_ENV, raising=False)
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    items = ["a", "b", "c", "d"]
    scores = [1.0, 4.0, 2.0, 3.0]
    result = prune_candidates(items, scores)
    assert isinstance(result, PruneResult)
    assert result.armed is False
    assert result.kept == items
    assert result.kept_indices == [0, 1, 2, 3]
    assert result.discarded_indices == []
    assert all(isinstance(d, PruneDecision) and d.keep for d in result.decisions)
    assert approx_eval_ready_to_prune() is False


def test_enforce_alone_does_not_prune(monkeypatch):
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, "1")
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    items = ["a", "b", "c", "d"]
    result = prune_candidates(items, [4, 3, 2, 1])
    assert result.armed is False
    assert result.kept == items


def test_armed_prune_keeps_top_half_higher_is_better(monkeypatch):
    _arm(monkeypatch)
    items = ["w", "x", "y", "z"]
    # scores: w=1, x=4, y=2, z=3 → keep x,z (top 2 of 4)
    result = prune_candidates(items, [1.0, 4.0, 2.0, 3.0])
    assert result.armed is True
    assert result.kept == ["x", "z"]
    assert result.kept_indices == [1, 3]
    assert set(result.discarded_indices) == {0, 2}
    by_index = {d.index: d for d in result.decisions}
    assert by_index[1].keep is True and by_index[1].approx_score == 4.0
    assert by_index[0].keep is False


def test_armed_prune_latency_lower_is_better(monkeypatch):
    _arm(monkeypatch)
    items = [
        {"id": "slow", "exec_time_ms": 40.0},
        {"id": "fast", "exec_time_ms": 10.0},
        {"id": "mid", "exec_time_ms": 20.0},
        {"id": "worse", "exec_time_ms": 50.0},
    ]
    scores = [40.0, 10.0, 20.0, 50.0]
    result = prune_candidates(items, scores, higher_is_better=False, keep_top_k=2)
    assert [c["id"] for c in result.kept] == ["fast", "mid"]
    assert result.decisions[1].item_id == "fast"
    assert result.decisions[1].keep is True


def test_keep_top_k_override(monkeypatch):
    _arm(monkeypatch)
    items = list(range(5))
    result = prune_candidates(items, [1, 5, 2, 4, 3], keep_top_k=1)
    assert result.kept == [1]
    assert result.kept_indices == [1]


def test_prune_never_returns_oracle_or_uplift_fields(monkeypatch):
    _arm(monkeypatch)
    result = prune_candidates(["a", "b", "c"], [3.0, 2.0, 1.0])
    assert not hasattr(result, "accepted")
    assert not hasattr(result, "improvement_pct")
    assert not hasattr(result, "eval_role")
    payload = result.__dict__
    assert "verdict" not in payload
    assert "public_uplift" not in payload


def test_refuse_approx_sourced_uplift_source_marker():
    with pytest.raises(ValueError, match="approx"):
        refuse_approx_sourced_uplift(
            {"accepted": True, "improvement_pct": 12.0, "source": "approx"}
        )


def test_refuse_approx_sourced_uplift_approx_score_field():
    with pytest.raises(ValueError, match="approx_score"):
        refuse_approx_sourced_uplift({"accepted": True, "approx_score": 0.9, "eval_role": "report"})


def test_refuse_approx_sourced_uplift_nested_report_public_uplift():
    with pytest.raises(ValueError, match="approx"):
        refuse_approx_sourced_uplift(
            {
                "success": True,
                "report_verdict": {
                    "eval_role": "report",
                    "public_uplift": {"improvement_pct": 1.0, "source": "approx"},
                },
            }
        )


def test_published_uplift_refuses_approx_even_when_flags_off(monkeypatch):
    monkeypatch.delenv(REPORT_UPLIFT_ENFORCE_ENV, raising=False)
    monkeypatch.delenv(APPROX_EVAL_ENFORCE_ENV, raising=False)
    with pytest.raises(ValueError, match="approx"):
        published_uplift_verdict(
            {
                "success": True,
                "iterations": [
                    {
                        "verdict": {
                            "accepted": True,
                            "improvement_pct": 9.0,
                            "source": "approx",
                        }
                    }
                ],
            }
        )


def test_published_uplift_refuses_approx_report_verdict(monkeypatch):
    monkeypatch.setenv(REPORT_UPLIFT_ENFORCE_ENV, "1")
    with pytest.raises(ValueError, match="approx"):
        published_uplift_verdict(
            {
                "success": True,
                "iterations": [{"verdict": {"accepted": True, "eval_role": "select"}}],
                "report_verdict": {
                    "accepted": True,
                    "eval_role": "report",
                    "improvement_pct": 1.0,
                    "approx_score": 0.42,
                },
            }
        )


def test_fingerprint_unchanged_leaves_latch(monkeypatch):
    _arm(monkeypatch)
    outcome = retest_agreement_on_change(
        "fp-a",
        [1.0, 2.0, 3.0, 4.0],
        [1.0, 2.0, 3.0, 4.0],
        previous_fingerprint="fp-a",
    )
    assert outcome.passed is True
    assert outcome.metric == "unchanged"
    assert approx_eval_armed() is True
    assert any("unchanged" in r for r in outcome.reasons)


def test_fingerprint_change_failed_agreement_disarms(monkeypatch):
    _arm(monkeypatch)
    assert approx_eval_armed() is True
    outcome = retest_agreement_on_change(
        "fp-b",
        [1.0, 2.0, 3.0, 4.0],
        [4.0, 3.0, 2.0, 1.0],
        previous_fingerprint="fp-a",
    )
    assert outcome.passed is False
    assert approx_eval_armed() is False


def test_fingerprint_change_passed_agreement_arms(monkeypatch):
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, "1")
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    outcome = retest_agreement_on_change(
        "fp-b",
        [1.0, 2.0, 3.0, 4.0],
        [10.0, 20.0, 30.0, 40.0],
        previous_fingerprint="fp-a",
    )
    assert outcome.passed is True
    assert approx_eval_armed() is True


def test_cold_start_previous_none_evaluates_and_may_disarm(monkeypatch):
    monkeypatch.setenv(APPROX_EVAL_ENFORCE_ENV, "1")
    monkeypatch.setenv(APPROX_EVAL_ARMED_ENV, "1")
    outcome = retest_agreement_on_change(
        "fp-new",
        [1.0, 2.0],
        [1.0, 2.0],
        previous_fingerprint=None,
    )
    assert outcome.passed is False
    assert math.isnan(outcome.coefficient) or outcome.n < 3
    assert approx_eval_armed() is False


def test_filter_ranked_library_noop_when_unarmed(monkeypatch):
    monkeypatch.delenv(APPROX_EVAL_ENFORCE_ENV, raising=False)
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    library = [
        {"id": "a", "exec_time_ms": 50.0, "code": "a"},
        {"id": "b", "exec_time_ms": 10.0, "code": "b"},
        {"id": "c", "exec_time_ms": 30.0, "code": "c"},
        {"id": "d", "exec_time_ms": 20.0, "code": "d"},
    ]
    assert filter_ranked_library(library) == library


def test_filter_ranked_library_prunes_when_armed(monkeypatch):
    _arm(monkeypatch)
    library = [
        {"id": "a", "exec_time_ms": 50.0, "code": "a"},
        {"id": "b", "exec_time_ms": 10.0, "code": "b"},
        {"id": "c", "exec_time_ms": 30.0, "code": "c"},
        {"id": "d", "exec_time_ms": 20.0, "code": "d"},
    ]
    kept = filter_ranked_library(library)
    assert [e["id"] for e in kept] == ["b", "d"]


def test_publish_kernel_uses_approx_prefilter_when_armed(tmp_path, monkeypatch):
    _arm(monkeypatch)
    from ultimate_coders.metainfer_service.execution import publish_kernel

    repository = tmp_path / "repository"
    state = tmp_path / "state"
    generated = tmp_path / "generated"
    for root in (repository, state, generated):
        root.mkdir()
    kernel = repository / "kernel.py"
    kernel.write_text("original kernel\n")
    (state / "run.json").write_text(json.dumps({"finished": True, "final_status": "success"}))
    # Upstream order puts a slow candidate first; armed prune should prefer fast.
    (generated / "kernel_library.json").write_text(
        json.dumps(
            [
                {"id": "slow", "code": "slow kernel\n", "exec_time_ms": 40.0},
                {"id": "fast", "code": "fast kernel\n", "exec_time_ms": 5.0},
                {"id": "mid", "code": "mid kernel\n", "exec_time_ms": 15.0},
                {"id": "worse", "code": "worse kernel\n", "exec_time_ms": 50.0},
            ]
        )
    )
    receipt = publish_kernel(repository, state, generated, kernel)
    assert kernel.read_text() == "fast kernel\n"
    assert receipt["candidate_id"] == "fast"


def test_publish_kernel_unchanged_when_unarmed(tmp_path, monkeypatch):
    monkeypatch.delenv(APPROX_EVAL_ENFORCE_ENV, raising=False)
    monkeypatch.delenv(APPROX_EVAL_ARMED_ENV, raising=False)
    from ultimate_coders.metainfer_service.execution import publish_kernel

    repository = tmp_path / "repository"
    state = tmp_path / "state"
    generated = tmp_path / "generated"
    for root in (repository, state, generated):
        root.mkdir()
    kernel = repository / "kernel.py"
    kernel.write_text("original kernel\n")
    (state / "run.json").write_text(json.dumps({"finished": True, "final_status": "success"}))
    (generated / "kernel_library.json").write_text(
        json.dumps(
            [
                {"id": "first", "code": "first kernel\n", "exec_time_ms": 40.0},
                {"id": "fast", "code": "fast kernel\n", "exec_time_ms": 5.0},
            ]
        )
    )
    receipt = publish_kernel(repository, state, generated, kernel)
    assert kernel.read_text() == "first kernel\n"
    assert receipt["candidate_id"] == "first"


def test_disarm_helper_still_clears_latch(monkeypatch):
    _arm(monkeypatch)
    disarm_approx_eval()
    assert approx_eval_armed() is False
    result = prune_candidates(["a", "b", "c"], [3, 2, 1])
    assert result.armed is False
    assert result.kept == ["a", "b", "c"]
