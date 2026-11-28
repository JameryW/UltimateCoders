"""PROBE failure brief for the next pipeline step (#70), production wiring.

The worker hands each downstream step a compact JSON view of the previous
step's output (``{{prev_outputs_json}}``). A failed step therefore reached the
next agent as a bare summary plus a truncated stderr tail: no anchored cause,
no failure location and no bounded recovery actions. That is precisely the raw
traceback replay that PROBE (arXiv:2605.08717) replaces with the four-stage
``Telemetry -> Evidence -> Diagnosis -> Guidance`` loop, and ORACLE-SWE
(arXiv:2604.07789) complements it with five required signal classes.

This module is the adapter between the two: it turns the failed
:class:`~ultimate_coders.agent.sandbox.AgentOutput` the worker already holds
into a :class:`~ultimate_coders.agent.probe_recovery.Telemetry` record, runs
:class:`~ultimate_coders.agent.probe_recovery.ProbeRecoveryEngine`, and returns
a bounded JSON-ready brief that the next step can read.

Enable with ``UC_PROBE_BRIEF=1``. Pure bookkeeping: deterministic, bounded,
and it never calls a model, shell or network.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from ultimate_coders.agent.probe_recovery import (
    ProbeRecoveryEngine,
    Telemetry,
    probe_recovery_enabled,
)

__all__ = [
    "FailureInputs",
    "MAX_ACTIONS",
    "MAX_BRIEF_CHARS",
    "PROBE_BRIEF_ENV",
    "build_telemetry",
    "failure_brief",
    "is_test_command",
    "probe_brief_enabled",
]

PROBE_BRIEF_ENV = "UC_PROBE_BRIEF"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Bound the brief: it rides inside ``{{prev_outputs_json}}`` next to the step
# summary, so an unbounded diagnosis would crowd out the actual result. The
# per-field budgets plus the final length check keep the encoded packet under
# ``MAX_BRIEF_CHARS`` even when every field is truncated at once.
MAX_ACTIONS = 3
MAX_BRIEF_CHARS = 1200
_MAX_TEXT = 120
_MAX_ACTION = 110
_MAX_NEXT_ATTEMPT = 240

# Minimal local hint list; kept independent of the completion-review gate so
# this adapter has no second dependency (both are pure string checks).
_TEST_COMMAND_HINTS = (
    "pytest",
    "py.test",
    "unittest",
    "cargo test",
    "cargo nextest",
    "npm test",
    "npm run test",
    "pnpm test",
    "yarn test",
    "bun test",
    "go test",
    "tox",
    "nox",
    "jest",
    "vitest",
    "rspec",
    "phpunit",
)


def probe_brief_enabled() -> bool:
    """True when ``UC_PROBE_BRIEF`` is truthy and the PROBE engine itself is on."""

    if os.environ.get(PROBE_BRIEF_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return probe_recovery_enabled()


def is_test_command(command: str) -> bool:
    """True when ``command`` looks like it executes a test suite."""

    lowered = command.strip().lower()
    if not lowered:
        return False
    return any(hint in lowered for hint in _TEST_COMMAND_HINTS)


def _clean(items: Any) -> tuple[str, ...]:
    """Normalize an iterable of strings into a non-empty stripped tuple."""

    if isinstance(items, str):
        items = (items,)
    cleaned: list[str] = []
    for item in items or ():
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return tuple(cleaned)


@dataclass(frozen=True)
class FailureInputs:
    """The bounded subset of a failed output that PROBE can reason about."""

    summary: str = ""
    stderr_tail: str = ""
    test_output: str = ""
    tool_calls: tuple[str, ...] = ()
    commands_run: tuple[str, ...] = ()
    file_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "stderr_tail", self.stderr_tail.strip())
        object.__setattr__(self, "test_output", self.test_output.strip())
        object.__setattr__(self, "tool_calls", _clean(self.tool_calls))
        object.__setattr__(self, "commands_run", _clean(self.commands_run))
        object.__setattr__(self, "file_paths", _clean(self.file_paths))

    @property
    def empty(self) -> bool:
        """True when the failure carries no signal at all."""

        return not (
            self.stderr_tail
            or self.test_output
            or self.tool_calls
            or self.commands_run
            or self.file_paths
        )

    @classmethod
    def from_output(cls, output: object) -> FailureInputs:
        """Build inputs from a duck-typed ``AgentOutput`` (or any stand-in)."""

        changes = getattr(output, "file_changes", ()) or ()
        paths: list[str] = []
        for change in changes:
            path = str(getattr(change, "file_path", "") or "").strip()
            if path:
                paths.append(path)
        return cls(
            summary=str(getattr(output, "summary", "") or ""),
            stderr_tail=str(getattr(output, "stderr_tail", "") or ""),
            test_output=str(getattr(output, "test_output", "") or ""),
            tool_calls=_clean(getattr(output, "tool_calls", ()) or ()),
            commands_run=_clean(getattr(output, "commands_run", ()) or ()),
            file_paths=tuple(paths),
        )


def build_telemetry(inputs: FailureInputs) -> Telemetry:
    """Project failure inputs onto PROBE's typed telemetry record.

    ``test_output`` (the last failed shell command's bounded output) is
    preferred over ``stderr_tail`` because it carries the assertion text the
    diagnosis anchors on; ``stderr_tail`` is the fallback for adapters that
    only report stderr.
    """

    stderr = inputs.test_output or inputs.stderr_tail
    tests = tuple(command for command in inputs.commands_run if is_test_command(command))
    command = inputs.commands_run[-1] if inputs.commands_run else ""
    tool = inputs.tool_calls[-1] if inputs.tool_calls else "sandbox"
    return Telemetry(
        tool=tool,
        command=command,
        stderr=stderr,
        touched_files=inputs.file_paths,
        test_names=tests[:2],
    )


def failure_brief(
    output: object,
    *,
    inputs: FailureInputs | None = None,
    max_actions: int = MAX_ACTIONS,
) -> dict[str, Any] | None:
    """Return the bounded PROBE brief for a failed output, or ``None``.

    ``None`` means "nothing to add": either the output carried no signal at
    all, or the inputs were supplied directly through ``inputs``. The brief is
    deliberately string/float only so it can be dropped straight into the
    worker's JSON payload.
    """

    inputs = inputs if inputs is not None else FailureInputs.from_output(output)
    if inputs.empty:
        return None
    engine = ProbeRecoveryEngine(max_actions=max(1, min(max_actions, 6)))
    report = engine.recover(build_telemetry(inputs))
    diagnosis = report.diagnosis
    brief: dict[str, Any] = {
        "main_cause": diagnosis.main_cause[:_MAX_TEXT],
        "failure_anchor": diagnosis.failure_anchor[:_MAX_TEXT],
        "behavior_error": diagnosis.behavior_error[:_MAX_TEXT],
        "confidence": round(diagnosis.confidence, 3),
        "signal_gaps": [kind.value for kind in report.gaps],
        "actions": [action[:_MAX_ACTION] for action in report.guidance.actions],
        "next_attempt": report.guidance.text[:_MAX_NEXT_ATTEMPT],
        "grounded": report.grounded,
    }
    return _fit(brief)


def _size(brief: dict[str, Any]) -> int:
    """Encoded size of ``brief`` (compact form, so the check is conservative)."""

    return len(json.dumps(brief, separators=(",", ":"), ensure_ascii=False))


def _fit(brief: dict[str, Any]) -> dict[str, Any]:
    """Shrink the brief until its encoded size respects ``MAX_BRIEF_CHARS``."""

    if _size(brief) <= MAX_BRIEF_CHARS:
        return brief
    brief["next_attempt"] = str(brief["next_attempt"])[: _MAX_NEXT_ATTEMPT // 2]
    if _size(brief) <= MAX_BRIEF_CHARS:
        return brief
    brief["actions"] = list(brief["actions"])[:1]
    if _size(brief) <= MAX_BRIEF_CHARS:
        return brief
    for key in ("main_cause", "failure_anchor", "behavior_error"):
        brief[key] = str(brief[key])[: _MAX_TEXT // 2]
    return brief
