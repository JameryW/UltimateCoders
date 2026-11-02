"""Watchdog input/output: deterministic evidence in, bounded recovery out.

The acceptance pinned here:

* the stall report is the exact ``watchdog.stall.report`` payload, with the
  progress detector's numbers copied rather than invented;
* ``apply_verdict`` maps every gateway verdict to one orchestrator action;
* a replan carries ``preserve_effects`` covering every confirmed effect id, so a
  recovery cannot repeat a confirmed external side effect -- and a replan that
  forgets one is refused, not executed;
* a second stall with the replan budget exhausted escalates instead of looping;
* the caps come from ``UC_WATCHDOG_MAX_REPLANS`` (2), ``UC_WATCHDOG_MAX_ATTEMPTS``
  (5) and ``UC_PROGRESS_STALL_LIMIT`` (2, the progress detector's semantics).
"""

from __future__ import annotations

import logging

import pytest
from ultimate_coders.agent.progress import ProgressVerdict
from ultimate_coders.proactive.watchdog_input import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_REPLANS,
    DEFAULT_STALL_LIMIT,
    ENV_PROGRESS_STALL_LIMIT,
    ENV_WATCHDOG_MAX_ATTEMPTS,
    ENV_WATCHDOG_MAX_REPLANS,
    VERDICT_ACTIONS,
    WatchdogAction,
    WatchdogContractError,
    WatchdogLimits,
    apply_verdict,
    build_stall_signal,
    normalize_stall_report,
    progress_stall_limit,
)

_STOP = ProgressVerdict(
    "stop",
    2,
    "no new evidence: same error signature, stderr and file set",
    "undefined symbol foo",
)
_RETRY = ProgressVerdict("retry", 1, "no new evidence", "undefined symbol foo")


# ── limits ───────────────────────────────────────────────────


def test_progress_stall_limit_matches_the_worker_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_PROGRESS_STALL_LIMIT, raising=False)
    assert progress_stall_limit() == DEFAULT_STALL_LIMIT == 2


@pytest.mark.parametrize(
    "raw, expected",
    [("3", 3), (" 4 ", 4), ("0", 1), ("-2", 1), ("not-a-number", 2), ("", 2)],
)
def test_progress_stall_limit_env_semantics(raw: str, expected: int) -> None:
    assert progress_stall_limit({ENV_PROGRESS_STALL_LIMIT: raw}) == expected


def test_watchdog_limits_default_to_the_gateway_config() -> None:
    limits = WatchdogLimits.from_env({})
    assert limits.stall_limit == 2
    assert limits.max_replans == DEFAULT_MAX_REPLANS == 2
    assert limits.max_attempts == DEFAULT_MAX_ATTEMPTS == 5
    assert limits.to_dict() == {"stall_limit": 2, "max_replans": 2, "max_attempts": 5}


def test_watchdog_limits_read_env_overrides() -> None:
    limits = WatchdogLimits.from_env(
        {
            ENV_WATCHDOG_MAX_REPLANS: "1",
            ENV_WATCHDOG_MAX_ATTEMPTS: "9",
            ENV_PROGRESS_STALL_LIMIT: "3",
        }
    )
    assert (limits.stall_limit, limits.max_replans, limits.max_attempts) == (3, 1, 9)


def test_watchdog_limits_fall_back_on_garbage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    environ = {ENV_WATCHDOG_MAX_REPLANS: "two", ENV_WATCHDOG_MAX_ATTEMPTS: "lots"}
    with caplog.at_level(logging.WARNING, logger="ultimate_coders.proactive.watchdog_input"):
        limits = WatchdogLimits.from_env(environ)
    assert (limits.max_replans, limits.max_attempts) == (
        DEFAULT_MAX_REPLANS,
        DEFAULT_MAX_ATTEMPTS,
    )
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 2


# ── stall report construction ────────────────────────────────


def test_build_stall_signal_payload_is_exact() -> None:
    payload = build_stall_signal(
        _STOP,
        task_id="task-1",
        subtask_id="s1",
        last_error="undefined symbol foo",
        confirmed_effect_ids=["eff_push", "eff_comment", "eff_push"],
        attempts_used=3,
        replans_used=1,
        already_retried=True,
        case_id="case-1",
        limits=WatchdogLimits(stall_limit=2),
    )
    assert payload == {
        "task_id": "task-1",
        "no_progress_count": 2,
        "confirmed_effect_ids": ["eff_push", "eff_comment"],
        "attempts_used": 3,
        "replans_used": 1,
        "already_retried": True,
        "subtask_id": "s1",
        "evidence_signature": "undefined symbol foo",
        "last_error": "undefined symbol foo",
        "case_id": "case-1",
    }


def test_build_stall_signal_omits_unset_optional_fields() -> None:
    unsigned = ProgressVerdict("stop", 2, "no new evidence", "")
    payload = build_stall_signal(unsigned, task_id="task-1", limits=WatchdogLimits(stall_limit=2))
    assert payload is not None
    assert set(payload) == {
        "task_id",
        "no_progress_count",
        "confirmed_effect_ids",
        "attempts_used",
        "replans_used",
        "already_retried",
    }
    assert payload["confirmed_effect_ids"] == []
    assert payload["already_retried"] is False


def test_build_stall_signal_skips_progress_below_the_stall_limit() -> None:
    payload = build_stall_signal(_RETRY, task_id="task-1", limits=WatchdogLimits(stall_limit=2))
    assert payload is None


def test_build_stall_signal_reports_at_the_stall_limit() -> None:
    at_limit = ProgressVerdict("retry", 2, "no new evidence", "boom")
    payload = build_stall_signal(at_limit, task_id="task-1", limits=WatchdogLimits(stall_limit=2))
    assert payload is not None
    assert payload["no_progress_count"] == 2


def test_build_stall_signal_always_reports_a_stop_verdict() -> None:
    early_stop = ProgressVerdict("stop", 1, "stop", "boom")
    payload = build_stall_signal(early_stop, task_id="task-1", limits=WatchdogLimits(stall_limit=2))
    assert payload is not None
    assert payload["no_progress_count"] == 1


def test_build_stall_signal_requires_a_task_id() -> None:
    with pytest.raises(ValueError, match="task_id"):
        build_stall_signal(_STOP, task_id="   ")


@pytest.mark.parametrize("counter", [-1, -5])
def test_build_stall_signal_rejects_negative_counters(counter: int) -> None:
    with pytest.raises(ValueError, match="attempts_used"):
        build_stall_signal(_STOP, task_id="task-1", attempts_used=counter)


def test_build_stall_signal_rejects_unrepresentable_effect_ids() -> None:
    with pytest.raises(ValueError, match="effect id"):
        build_stall_signal(_STOP, task_id="task-1", confirmed_effect_ids=[123])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="non-empty"):
        build_stall_signal(_STOP, task_id="task-1", confirmed_effect_ids=["  "])
    with pytest.raises(ValueError, match="sequence"):
        build_stall_signal(_STOP, task_id="task-1", confirmed_effect_ids="eff_push")


def test_build_stall_signal_rejects_an_unknown_progress_action() -> None:
    bogus = ProgressVerdict("replan", 2, "invented", "boom")
    with pytest.raises(ValueError, match="unknown progress verdict action"):
        build_stall_signal(bogus, task_id="task-1")


def test_normalize_stall_report_matches_build_stall_signal() -> None:
    body = {
        "task_id": "task-1",
        "subtask_id": "s1",
        "no_progress_count": 2,
        "evidence_signature": "undefined symbol foo",
        "last_error": "undefined symbol foo",
        "confirmed_effect_ids": ["eff_push", "eff_push"],
        "attempts_used": 3,
        "replans_used": 1,
        "already_retried": True,
        "case_id": "case-1",
    }
    assert normalize_stall_report(body) == build_stall_signal(
        _STOP,
        task_id="task-1",
        subtask_id="s1",
        last_error="undefined symbol foo",
        confirmed_effect_ids=["eff_push"],
        attempts_used=3,
        replans_used=1,
        already_retried=True,
        case_id="case-1",
        limits=WatchdogLimits(stall_limit=2),
    )


def test_normalize_stall_report_validates_shape() -> None:
    with pytest.raises(ValueError, match="task_id"):
        normalize_stall_report({})
    with pytest.raises(ValueError, match="boolean"):
        normalize_stall_report({"task_id": "t", "already_retried": "yes"})
    with pytest.raises(ValueError, match="no_progress_count"):
        normalize_stall_report({"task_id": "t", "no_progress_count": "two"})
    with pytest.raises(ValueError, match="must be an object"):
        normalize_stall_report(["not", "a", "mapping"])  # type: ignore[arg-type]


# ── verdict mapping ──────────────────────────────────────────


def test_verdict_tokens_cover_the_gateway_enum() -> None:
    assert set(VERDICT_ACTIONS) == {
        "continue",
        "retry_same_contract",
        "replan",
        "escalate_needs_human",
        "stop",
    }
    assert VERDICT_ACTIONS["escalate_needs_human"] == "escalate"


def test_apply_verdict_continue() -> None:
    action = apply_verdict({"verdict": "continue", "replans_remaining": 2, "attempts_remaining": 4})
    assert isinstance(action, WatchdogAction)
    assert action.action == "continue"
    assert action.delay_ms == 0
    assert action.preserve_effects == ()
    assert (action.replans_remaining, action.attempts_remaining) == (2, 4)


def test_apply_verdict_retry_same_contract_carries_the_delay() -> None:
    action = apply_verdict(
        {
            "verdict": "retry_same_contract",
            "delay_ms": 1000,
            "replans_remaining": 2,
            "attempts_remaining": 3,
        }
    )
    assert action.action == "retry_same_contract"
    assert action.delay_ms == 1000
    assert action.preserve_effects == ()


def test_apply_verdict_replan_carries_preserve_effects() -> None:
    action = apply_verdict(
        {
            "verdict": "replan",
            "attempt": 1,
            "delay_ms": 1000,
            "preserve_effects": ["eff_push", "eff_comment"],
            "replans_remaining": 1,
            "attempts_remaining": 3,
        },
        confirmed_effect_ids=["eff_push", "eff_comment"],
    )
    assert action.action == "replan"
    assert action.attempt == 1
    assert action.delay_ms == 1000
    assert action.preserve_effects == ("eff_push", "eff_comment")
    assert action.to_dict()["preserve_effects"] == ["eff_push", "eff_comment"]


def test_replan_missing_a_confirmed_effect_is_refused() -> None:
    with pytest.raises(WatchdogContractError, match="eff_push"):
        apply_verdict(
            {
                "verdict": "replan",
                "attempt": 1,
                "delay_ms": 1000,
                "preserve_effects": ["eff_comment"],
                "replans_remaining": 1,
                "attempts_remaining": 3,
            },
            confirmed_effect_ids=["eff_push", "eff_comment"],
        )


def test_replan_without_replan_budget_is_refused() -> None:
    with pytest.raises(WatchdogContractError, match="no replan budget"):
        apply_verdict(
            {
                "verdict": "replan",
                "attempt": 1,
                "delay_ms": 1000,
                "preserve_effects": [],
                "replans_remaining": 0,
                "attempts_remaining": 3,
            }
        )


def test_replan_without_an_attempt_number_is_refused() -> None:
    with pytest.raises(WatchdogContractError, match="attempt"):
        apply_verdict(
            {
                "verdict": "replan",
                "delay_ms": 1000,
                "preserve_effects": [],
                "replans_remaining": 1,
                "attempts_remaining": 3,
            }
        )


def test_apply_verdict_escalates_with_the_reason() -> None:
    action = apply_verdict(
        {
            "verdict": "escalate_needs_human",
            "reason": "stalled for 2 attempts and the replan budget is exhausted",
            "replans_remaining": 0,
            "attempts_remaining": 2,
        }
    )
    assert action.action == "escalate"
    assert action.preserve_effects == ()
    assert "replan budget is exhausted" in (action.reason or "")


def test_apply_verdict_stops_with_the_reason() -> None:
    action = apply_verdict(
        {
            "verdict": "stop",
            "reason": "attempt budget exhausted (5/5)",
            "replans_remaining": 0,
            "attempts_remaining": 0,
        }
    )
    assert action.action == "stop"
    assert "attempt budget" in (action.reason or "")


@pytest.mark.parametrize("verdict", ["retry", "replanning", "", None, 3])
def test_unknown_verdicts_raise_instead_of_continuing(verdict: object) -> None:
    with pytest.raises(WatchdogContractError, match="unknown watchdog verdict"):
        apply_verdict({"verdict": verdict})


def test_non_mapping_verdict_raises() -> None:
    with pytest.raises(WatchdogContractError):
        apply_verdict("continue")  # type: ignore[arg-type]


@pytest.mark.parametrize("counter", [True, "1", -1, 1.5])
def test_malformed_counters_raise(counter: object) -> None:
    with pytest.raises(WatchdogContractError):
        apply_verdict({"verdict": "continue", "replans_remaining": counter})


def test_second_stall_with_the_replan_budget_exhausted_escalates() -> None:
    """The loop must end: the first stall replans, the second escalates."""
    first = apply_verdict(
        {
            "verdict": "replan",
            "attempt": 1,
            "delay_ms": 1000,
            "preserve_effects": ["eff_push"],
            "replans_remaining": 1,
            "attempts_remaining": 3,
        },
        confirmed_effect_ids=["eff_push"],
    )
    assert first.action == "replan"
    second = apply_verdict(
        {
            "verdict": "escalate_needs_human",
            "reason": "stalled for 2 attempts and the replan budget is exhausted",
            "replans_remaining": 0,
            "attempts_remaining": 2,
        },
        confirmed_effect_ids=["eff_push"],
    )
    assert second.action == "escalate"
    assert second.action != "replan", "an exhausted replan budget must not loop"
    assert second.replans_remaining == 0


def test_watchdog_action_rejects_an_unknown_action() -> None:
    with pytest.raises(ValueError, match="unknown watchdog action"):
        WatchdogAction(action="redispatch")


def test_watchdog_action_to_dict_shape() -> None:
    action = WatchdogAction(
        action="replan",
        delay_ms=2000,
        preserve_effects=("eff_push",),
        attempt=2,
        reason="bounded replan",
        replans_remaining=0,
        attempts_remaining=1,
    )
    assert action.to_dict() == {
        "action": "replan",
        "delay_ms": 2000,
        "preserve_effects": ["eff_push"],
        "replans_remaining": 0,
        "attempts_remaining": 1,
        "attempt": 2,
        "reason": "bounded replan",
    }
