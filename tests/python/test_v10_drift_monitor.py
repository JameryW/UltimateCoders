"""Tests for the v10 DriftMonitor module (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.drift_monitor import (
    DRIFT_MONITOR_ENV,
    DriftMonitor,
    DriftSignal,
    drift_monitor_enabled,
)


def test_edit_failures_trigger_and_success_resets() -> None:
    monitor = DriftMonitor(edit_failure_threshold=2)
    assert monitor.observe_edit(False) is False
    assert monitor.triggered() == ()
    assert monitor.observe_edit(False) is True
    assert monitor.triggered() == (DriftSignal.EDIT_FAILURES,)

    monitor.observe_edit(True)
    monitor.observe_edit(False)
    assert monitor.state.consecutive_edit_failures == 1


def test_outside_boundary_trigger() -> None:
    monitor = DriftMonitor(outside_boundary_threshold=2)
    assert monitor.observe_step("read", path_in_boundary=False, within_two_hop=False) is False
    assert monitor.observe_step("edit", path_in_boundary=True, within_two_hop=False) is False
    assert monitor.observe_step("edit", path_in_boundary=False, within_two_hop=False) is False
    assert monitor.observe_step("edit", path_in_boundary=False, within_two_hop=False) is True
    assert DriftSignal.OUTSIDE_BOUNDARY in monitor.triggered()


def test_near_boundary_is_not_a_hard_signal() -> None:
    monitor = DriftMonitor(outside_boundary_threshold=1)
    assert monitor.observe_step("edit", path_in_boundary=False, within_two_hop=True) is False
    assert monitor.triggered() == ()
    assert monitor.state.near_boundary_steps == 1


def test_stack_trace_repeat_ignores_line_numbers() -> None:
    monitor = DriftMonitor(stack_trace_repeat_threshold=2)
    first = 'Traceback (most recent call last):\n  File "a.py", line 10, in run\nValueError: bad'
    second = 'Traceback (most recent call last):\n  File "a.py", line 99, in run\nValueError: bad'
    assert monitor.observe_test(first, net_diff_lines=3) is False
    assert monitor.observe_test(second, net_diff_lines=3) is True
    assert DriftSignal.STACK_TRACE_REPEAT in monitor.triggered()


def test_different_trace_does_not_repeat() -> None:
    monitor = DriftMonitor(stack_trace_repeat_threshold=2)
    monitor.observe_test("ValueError: one", net_diff_lines=1)
    assert monitor.observe_test("TypeError: two", net_diff_lines=1) is False
    assert monitor.triggered() == ()


def test_budget_zero_coverage_trigger() -> None:
    monitor = DriftMonitor(budget_zero_coverage_threshold=1)
    assert (
        monitor.observe_budget(
            step_index=1,
            total_steps=5,
            required_files=["a.py"],
            covered_files=[],
        )
        is False
    )
    assert (
        monitor.observe_budget(
            step_index=5,
            total_steps=5,
            required_files=["a.py"],
            covered_files=[],
        )
        is True
    )
    assert DriftSignal.BUDGET_ZERO_COVERAGE in monitor.triggered()


def test_budget_with_coverage_does_not_trigger() -> None:
    monitor = DriftMonitor()
    assert (
        monitor.observe_budget(
            step_index=5,
            total_steps=5,
            required_files=["a.py"],
            covered_files=["a.py"],
        )
        is False
    )
    assert monitor.triggered() == ()


def test_no_trigger_means_no_prompt() -> None:
    monitor = DriftMonitor()
    assert monitor.triggered() == ()
    assert monitor.advisor_prompt() == ""


def test_one_advisor_prompt_per_episode_and_word_cap() -> None:
    monitor = DriftMonitor(
        edit_failure_threshold=1,
        outside_boundary_threshold=1,
        stack_trace_repeat_threshold=1,
        budget_zero_coverage_threshold=1,
    )
    monitor.observe_edit(False)
    monitor.observe_step("edit", path_in_boundary=False, within_two_hop=False)
    monitor.observe_test("ValueError: boom", net_diff_lines=0)
    monitor.observe_budget(3, 3, ["a.py"], [])

    prompt = monitor.advisor_prompt(max_words=25)
    assert prompt
    assert len(prompt.split()) <= 25
    assert monitor.advisor_prompt() == ""

    monitor.reset()
    assert monitor.triggered() == ()
    assert monitor.state.prompt_emitted is False


def test_thresholds_mapping_override() -> None:
    monitor = DriftMonitor({DriftSignal.EDIT_FAILURES: 1})
    assert monitor.observe_edit(False) is True


def test_invalid_threshold_rejected() -> None:
    with pytest.raises(ValueError):
        DriftMonitor(edit_failure_threshold=0)
    with pytest.raises(ValueError):
        DriftMonitor().advisor_prompt(max_words=0)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_drift_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(DRIFT_MONITOR_ENV, value)
    assert drift_monitor_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
def test_drift_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(DRIFT_MONITOR_ENV, value)
    assert drift_monitor_enabled() is False
