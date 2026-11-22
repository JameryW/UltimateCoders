"""Drift monitor: deterministic early-warning signals for a stuck agent.

DriftMonitor (#16) watches a small set of observable behaviours and raises at
most four deterministic signals:

* repeated edit failures,
* edits that leave the task boundary,
* the same stack trace recurring across test runs,
* a spent step budget with zero required-file coverage.

The monitor is pure bookkeeping -- no timers, no randomness, no LLM calls.  A
supervisor can poll :meth:`DriftMonitor.triggered`, and when any signal is
live pull exactly one short :meth:`DriftMonitor.advisor_prompt` to steer the
agent back on track.  The prompt is emitted once per drift episode; call
:meth:`DriftMonitor.reset` to start a new episode.

The whole feature is opt-in and default-off (``UC_DRIFT_MONITOR``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")

__all__ = [
    "DRIFT_MONITOR_ENV",
    "DriftMonitor",
    "DriftSignal",
    "DriftState",
    "drift_monitor_enabled",
]

DRIFT_MONITOR_ENV = "UC_DRIFT_MONITOR"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_EDIT_STEP_KINDS = frozenset({"edit", "write", "patch", "apply_patch"})


def drift_monitor_enabled() -> bool:
    """Feature flag: the drift monitor is enabled only when ``UC_DRIFT_MONITOR`` is truthy."""

    return os.environ.get(DRIFT_MONITOR_ENV, "").strip().lower() in _TRUTHY


class DriftSignal(str, Enum):
    """The four deterministic drift signals."""

    EDIT_FAILURES = "edit_failures"
    OUTSIDE_BOUNDARY = "outside_boundary"
    STACK_TRACE_REPEAT = "stack_trace_repeat"
    BUDGET_ZERO_COVERAGE = "budget_zero_coverage"


_SIGNAL_ORDER: tuple[DriftSignal, ...] = (
    DriftSignal.EDIT_FAILURES,
    DriftSignal.OUTSIDE_BOUNDARY,
    DriftSignal.STACK_TRACE_REPEAT,
    DriftSignal.BUDGET_ZERO_COVERAGE,
)

_SIGNAL_LABELS = {
    DriftSignal.EDIT_FAILURES: "repeated edit failures",
    DriftSignal.OUTSIDE_BOUNDARY: "edit outside the task boundary",
    DriftSignal.STACK_TRACE_REPEAT: "the same stack trace repeating",
    DriftSignal.BUDGET_ZERO_COVERAGE: "step budget spent with zero required-file coverage",
}

_SIGNAL_ADVICE = {
    DriftSignal.EDIT_FAILURES: (
        "Stop editing: read the current file state, then make one small change instead of "
        "retrying the same patch."
    ),
    DriftSignal.OUTSIDE_BOUNDARY: (
        "Return to the files in scope and justify any cross-module edit before making it."
    ),
    DriftSignal.STACK_TRACE_REPEAT: (
        "The failure is not changing; inspect the trace, form one new hypothesis, and test it "
        "before touching more code."
    ),
    DriftSignal.BUDGET_ZERO_COVERAGE: (
        "Cover at least one required file now, or stop and report the blocker instead of "
        "spending more steps."
    ),
}


@dataclass
class DriftState:
    """Mutable bookkeeping for one drift-monitoring episode."""

    edit_attempts: int = 0
    edit_failures: int = 0
    consecutive_edit_failures: int = 0
    outside_boundary_steps: int = 0
    near_boundary_steps: int = 0
    observed_test_traces: int = 0
    repeated_traces: int = 0
    budget_checks: int = 0
    zero_coverage_checks: int = 0
    signals: set[DriftSignal] = field(default_factory=set)
    prompt_emitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of this state."""

        return {
            "edit_attempts": self.edit_attempts,
            "edit_failures": self.edit_failures,
            "consecutive_edit_failures": self.consecutive_edit_failures,
            "outside_boundary_steps": self.outside_boundary_steps,
            "near_boundary_steps": self.near_boundary_steps,
            "observed_test_traces": self.observed_test_traces,
            "repeated_traces": self.repeated_traces,
            "budget_checks": self.budget_checks,
            "zero_coverage_checks": self.zero_coverage_checks,
            "signals": [signal.value for signal in _SIGNAL_ORDER if signal in self.signals],
            "prompt_emitted": self.prompt_emitted,
        }


class DriftMonitor:
    """Accumulate drift evidence and emit one bounded advisor prompt.

    Thresholds are injected per signal so callers can tune strictness; all
    counters are monotonic until :meth:`reset`.  Each ``observe_*`` method
    returns ``True`` when it raises a signal that was not already live.
    """

    def __init__(
        self,
        thresholds: Mapping[DriftSignal | str, int] | None = None,
        *,
        edit_failure_threshold: int = 3,
        outside_boundary_threshold: int = 2,
        stack_trace_repeat_threshold: int = 3,
        budget_zero_coverage_threshold: int = 1,
    ) -> None:
        resolved = {
            DriftSignal.EDIT_FAILURES: int(edit_failure_threshold),
            DriftSignal.OUTSIDE_BOUNDARY: int(outside_boundary_threshold),
            DriftSignal.STACK_TRACE_REPEAT: int(stack_trace_repeat_threshold),
            DriftSignal.BUDGET_ZERO_COVERAGE: int(budget_zero_coverage_threshold),
        }
        for key, value in dict(thresholds or {}).items():
            signal = key if isinstance(key, DriftSignal) else DriftSignal(str(key))
            resolved[signal] = int(value)
        for signal, value in resolved.items():
            if value < 1:
                raise ValueError(f"threshold for {signal.value} must be >= 1")
        self.thresholds: dict[DriftSignal, int] = resolved
        self.state = DriftState()
        self._last_trace = ""

    # ── observation hooks ────────────────────────────────────────

    def observe_edit(self, success: bool) -> bool:
        """Record one edit attempt; repeated consecutive failures raise a signal."""

        self.state.edit_attempts += 1
        if success:
            self.state.consecutive_edit_failures = 0
            return False
        self.state.edit_failures += 1
        self.state.consecutive_edit_failures += 1
        if self.state.consecutive_edit_failures >= self.thresholds[DriftSignal.EDIT_FAILURES]:
            return self._raise(DriftSignal.EDIT_FAILURES)
        return False

    def observe_step(
        self,
        kind: str,
        path_in_boundary: bool,
        within_two_hop: bool,
    ) -> bool:
        """Record one step; boundary escapes raise ``OUTSIDE_BOUNDARY``.

        Only edit-like steps can drift out of bounds.  A file outside the
        explicit boundary but inside the two-hop dependency neighbourhood is
        tracked as near-boundary work and does not raise the hard signal.
        """

        step_kind = str(kind or "").strip().lower()
        if step_kind not in _EDIT_STEP_KINDS:
            return False
        if path_in_boundary:
            return False
        if within_two_hop:
            self.state.near_boundary_steps += 1
            return False
        self.state.outside_boundary_steps += 1
        if self.state.outside_boundary_steps >= self.thresholds[DriftSignal.OUTSIDE_BOUNDARY]:
            return self._raise(DriftSignal.OUTSIDE_BOUNDARY)
        return False

    def observe_test(self, trace: str, net_diff_lines: int) -> bool:
        """Record a test failure trace; an unchanged trace raises ``STACK_TRACE_REPEAT``.

        ``net_diff_lines`` is recorded only as context for the advisor prompt:
        a failing test after a zero-line diff is especially likely to be a
        loop, but the trace identity is what makes the signal deterministic.
        """

        normalized = _normalize_trace(trace)
        self.state.observed_test_traces += 1
        if not normalized:
            self._last_trace = ""
            self.state.repeated_traces = 0
            return False
        if normalized == self._last_trace:
            self.state.repeated_traces += 1
        else:
            self._last_trace = normalized
            self.state.repeated_traces = 1
        if self.state.repeated_traces >= self.thresholds[DriftSignal.STACK_TRACE_REPEAT]:
            return self._raise(DriftSignal.STACK_TRACE_REPEAT)
        return False

    def observe_budget(
        self,
        step_index: int,
        total_steps: int,
        required_files: Any,
        covered_files: Any,
    ) -> bool:
        """Record budget/coverage; exhausted budget with no required coverage raises a signal."""

        self.state.budget_checks += 1
        req_seq = [required_files] if isinstance(required_files, str) else (required_files or ())
        cov_seq = [covered_files] if isinstance(covered_files, str) else (covered_files or ())
        required = {str(item) for item in req_seq if str(item)}
        covered = {str(item) for item in cov_seq if str(item)}
        exhausted = int(total_steps) > 0 and int(step_index) >= int(total_steps)
        if not exhausted:
            return False
        if required:
            covered_required = required & covered
        else:
            covered_required = covered
        if covered_required:
            return False
        self.state.zero_coverage_checks += 1
        if self.state.zero_coverage_checks >= self.thresholds[DriftSignal.BUDGET_ZERO_COVERAGE]:
            return self._raise(DriftSignal.BUDGET_ZERO_COVERAGE)
        return False

    # ── reporting ────────────────────────────────────────────────

    def triggered(self) -> tuple[DriftSignal, ...]:
        """Return live signals in deterministic declaration order."""

        return tuple(signal for signal in _SIGNAL_ORDER if signal in self.state.signals)

    def advisor_prompt(self, max_words: int = 150) -> str:
        """Return one short advisor prompt, or ``""`` when not triggered.

        The prompt is emitted at most once per episode even if drift keeps
        accumulating; :meth:`reset` clears the latch.
        """

        if max_words < 1:
            raise ValueError("max_words must be >= 1")
        signals = self.triggered()
        if not signals or self.state.prompt_emitted:
            return ""
        self.state.prompt_emitted = True
        observed = ", ".join(_SIGNAL_LABELS[signal] for signal in signals)
        advice = " ".join(_SIGNAL_ADVICE[signal] for signal in signals)
        prompt = (
            f"Drift detected: {observed}. {advice} "
            "Re-read the task, state the smallest next step, and verify it before continuing."
        )
        return _truncate_words(prompt, max_words)

    def reset(self) -> None:
        """Clear all counters, signals, and the prompt latch for a new episode."""

        self.state = DriftState()
        self._last_trace = ""

    def _raise(self, signal: DriftSignal) -> bool:
        if signal in self.state.signals:
            return False
        self.state.signals.add(signal)
        return True


def _normalize_trace(trace: str) -> str:
    """Normalize a stack trace for deterministic equality.

    File paths, line numbers, memory addresses, and timings vary between runs
    of the same failure; collapsing whitespace and stripping common volatile
    fragments keeps the identity stable without interpreting the trace.
    """

    lines: list[str] = []
    for raw_line in str(trace or "").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if line.startswith("File "):
            line = _normalize_file_line(line)
        line = _HEX_ADDR_RE.sub("0xADDR", line)
        lines.append(line)
    return "\n".join(lines)


def _normalize_file_line(line: str) -> str:
    """Strip ``line N`` from a Python ``File "..."`` frame while keeping the path."""

    marker = line.find(", line ")
    if marker < 0:
        return line
    tail = line[marker + len(", line ") :]
    end = 0
    while end < len(tail) and tail[end].isdigit():
        end += 1
    if end == 0:
        return line
    return line[:marker] + tail[end:]


def _truncate_words(text: str, max_words: int) -> str:
    words = str(text or "").split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])
