"""Tests for the bounded Recovery View (knowledge/recovery.py).

The view is the retry-loop half of the E2 "Recovery View + Integration
Receipt" phase: it renders committed prior-attempt facts into a small,
log-safe prompt section. These tests pin the invariants the worker wiring
depends on:

* facts are verified items and hypotheses are not (principle A);
* the view is attempt-local (never claims cross-graph knowledge);
* rendering is bounded by the token budget and drops the oldest rounds first;
* malformed ledger records never crash the retry path.
"""

from __future__ import annotations

from ultimate_coders.agent.context_compiler import Tier, estimate_tokens
from ultimate_coders.knowledge.model import AttemptRef
from ultimate_coders.knowledge.recovery import (
    DEFAULT_MAX_ROUNDS,
    MAX_HYPOTHESES,
    MAX_PATHS_PER_ROUND,
    MAX_ROUND_DETAIL,
    RECOVERY_TITLE,
    build_recovery_view,
    recovery_context_items,
)


def _attempt(retry: int = 1, dispatch: int = 1) -> AttemptRef:
    return AttemptRef(
        task_id="t1",
        subtask_id="subtask1",
        dispatch_attempt=dispatch,
        attempt=retry,
    )


def _round(
    retry: int = 1,
    *,
    outcome: str = "failed",
    paths: tuple[str, ...] = ("src/a.py",),
    failure: str = "AssertionError: boom",
    verdict: str = "failed",
    summary: str = "",
    base: str = "",
    head: str = "",
) -> dict:
    return {
        "attempt": _attempt(retry=retry, dispatch=retry).to_dict(),
        "outcome": outcome,
        "patches": [{"path": path} for path in paths],
        "failure": failure,
        "test_verdict": verdict,
        "base_revision": base,
        "head_revision": head,
        "summary": summary,
    }


def test_empty_inputs_produce_an_empty_view():
    view = build_recovery_view(attempt=_attempt())

    assert view.is_empty()
    assert view.text == ""
    assert view.tokens() == 0
    assert view.summary["rounds_shown"] == 0
    assert view.summary["rounds_available"] == 0
    assert view.summary["evidence_records"] == 0
    assert view.summary["hypotheses_shown"] == 0
    assert view.summary["truncated"] is False
    assert recovery_context_items(view) == []


def test_renders_prior_round_facts():
    view = build_recovery_view(
        attempt=_attempt(retry=2),
        rounds=[_round(retry=1, base="a" * 40, head="b" * 40)],
    )

    text = view.text
    assert text.startswith(RECOVERY_TITLE)
    assert "### Attempt 1 (dispatch 1) of subtask subtask1" in text
    assert "Changed: src/a.py" in text
    assert f"Revision: {'a' * 12} -> {'b' * 12}" in text
    assert "Failure: AssertionError: boom" in text
    assert "Verification: failed" in text

    assert view.summary["rounds_shown"] == 1
    assert view.summary["rounds_available"] == 1
    assert view.summary["attempt"] == "t1:subtask1:1:2"
    assert view.summary["tokens"] == estimate_tokens(text)
    assert view.summary["truncated"] is False


def test_hypotheses_are_rendered_but_never_verified():
    view = build_recovery_view(
        attempt=_attempt(retry=2),
        rounds=[_round()],
        hypotheses=[
            {"statement": "the cache key is stale"},
            {"statement": "the parser drops the trailing comma"},
        ],
    )

    assert "Unverified hypotheses from prior attempts" in view.text
    assert "the cache key is stale" in view.text
    assert view.summary["hypotheses_shown"] == 2

    facts_item, hypothesis_item = recovery_context_items(view)
    assert facts_item.tier == Tier.EVIDENCE
    assert facts_item.verified is True
    assert facts_item.trust_class == "attempt_ledger"
    assert hypothesis_item.tier == Tier.EVIDENCE
    assert hypothesis_item.verified is False
    assert hypothesis_item.trust_class == "unverified_hypothesis"
    assert facts_item.item_id == "recovery-facts:t1:subtask1:1:2"
    assert hypothesis_item.item_id == "recovery-hypotheses:t1:subtask1:1:2"


def test_hypotheses_are_capped():
    hypotheses = [{"statement": f"guess {index}"} for index in range(MAX_HYPOTHESES + 3)]

    view = build_recovery_view(attempt=_attempt(), rounds=[_round()], hypotheses=hypotheses)

    assert view.summary["hypotheses_shown"] == MAX_HYPOTHESES
    assert "guess 4" not in view.text


def test_recent_rounds_win_and_old_ones_are_dropped():
    rounds = [_round(retry=index) for index in range(1, 7)]

    view = build_recovery_view(
        attempt=_attempt(retry=7), rounds=rounds, max_rounds=2, budget_tokens=2000
    )

    assert "### Attempt 6 " in view.text
    assert "### Attempt 5 " in view.text
    assert "### Attempt 1 " not in view.text
    assert view.summary["rounds_shown"] == 2
    assert view.summary["rounds_available"] == 6
    assert view.summary["truncated"] is True


def test_budget_bounds_the_rendered_text():
    rounds = [_round(retry=index, failure="E" * 500) for index in range(1, 8)]

    view = build_recovery_view(
        attempt=_attempt(retry=8), rounds=rounds, budget_tokens=128, max_rounds=7
    )

    assert view.summary["budget_tokens"] == 128
    assert view.summary["truncated"] is True
    assert len(view.text) <= 128 * 4


def test_changes_paths_collapse_beyond_the_limit():
    paths = tuple(f"src/f{index}.py" for index in range(MAX_PATHS_PER_ROUND + 2))

    view = build_recovery_view(attempt=_attempt(), rounds=[_round(paths=paths)])

    assert "Changed: src/f0.py, src/f1.py" in view.text
    assert f"... and {2} more" in view.text
    assert "src/f5.py" in view.text
    assert "src/f6.py" not in view.text


def test_malformed_records_are_ignored_not_fatal():
    view = build_recovery_view(
        attempt=_attempt(),
        rounds=[None, "not-a-round", {"attempt": "junk", "outcome": "failed"}],
        evidence=[None, {"kind": "patch_observed"}],
        hypotheses=[None, {"statement": ""}],
    )

    assert "### junk — failed" in view.text
    assert view.summary["rounds_available"] == 1
    assert view.summary["evidence_records"] == 1
    assert view.summary["hypotheses_shown"] == 0


def test_long_free_text_is_capped():
    view = build_recovery_view(
        attempt=_attempt(),
        rounds=[_round(failure="X" * (MAX_ROUND_DETAIL * 3), summary="Y" * (MAX_ROUND_DETAIL * 3))],
        budget_tokens=2000,
    )

    assert "Failure: " + "X" * (MAX_ROUND_DETAIL - 3) + "..." in view.text
    assert "X" * (MAX_ROUND_DETAIL + 1) not in view.text


def test_round_trips_through_dict():
    view = build_recovery_view(
        attempt=_attempt(retry=3),
        rounds=[_round(retry=2)],
        hypotheses=[{"statement": "stale cache"}],
    )

    restored = type(view).from_dict(view.to_dict())

    assert restored.text == view.text
    assert restored.summary == view.summary
    assert restored.attempt.key() == view.attempt.key()


def test_default_round_limit_matches_constant():
    rounds = [_round(retry=index) for index in range(1, DEFAULT_MAX_ROUNDS + 4)]

    view = build_recovery_view(attempt=_attempt(retry=20), rounds=rounds, budget_tokens=5000)

    assert view.summary["rounds_shown"] == DEFAULT_MAX_ROUNDS

