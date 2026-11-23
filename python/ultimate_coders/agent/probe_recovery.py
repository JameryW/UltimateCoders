"""PROBE failure-anchored structured recovery (#70), with ORACLE-SWE signal gaps.

The ledger's PROBE item replaces raw traceback replay with a controlled loop:
Telemetry -> structured Evidence -> structured Diagnosis -> bounded Guidance.
ORACLE-SWE contributes five required signal classes; a diagnosis is only
released through the guidance gate when its cause, failure anchor, behavioral
error and confidence are grounded in collected evidence.

Enable with ``UC_PROBE_RECOVERY=1``. The module is pure bookkeeping and never
calls a model, shell or network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Diagnosis",
    "Evidence",
    "Guidance",
    "GuidanceGate",
    "OracleSignals",
    "ProbeRecoveryEngine",
    "RecoveryReport",
    "SignalKind",
    "Telemetry",
    "probe_recovery_enabled",
]

PROBE_RECOVERY_ENV = "UC_PROBE_RECOVERY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def probe_recovery_enabled() -> bool:
    return os.environ.get(PROBE_RECOVERY_ENV, "").strip().lower() in _TRUTHY


class SignalKind(str, Enum):
    REPRODUCTION = "reproduction"
    REGRESSION = "regression"
    LOCATION = "location"
    CONTEXT = "context"
    API_USAGE = "api_usage"


@dataclass(frozen=True)
class Telemetry:
    """Typed telemetry captured across one failed attempt."""

    tool: str
    command: str = ""
    stderr: str = ""
    touched_files: tuple[str, ...] = ()
    test_names: tuple[str, ...] = ()
    context_notes: tuple[str, ...] = ()
    api_calls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.tool.strip():
            raise ValueError("tool must not be empty")


@dataclass(frozen=True)
class Evidence:
    """Structured evidence extracted from telemetry."""

    reproduction: tuple[str, ...] = ()
    regression: tuple[str, ...] = ()
    location: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    api_usage: tuple[str, ...] = ()

    def items(self, kind: SignalKind) -> tuple[str, ...]:
        return {
            SignalKind.REPRODUCTION: self.reproduction,
            SignalKind.REGRESSION: self.regression,
            SignalKind.LOCATION: self.location,
            SignalKind.CONTEXT: self.context,
            SignalKind.API_USAGE: self.api_usage,
        }[kind]

    def has(self, kind: SignalKind) -> bool:
        return bool(self.items(kind))

    @property
    def kinds(self) -> frozenset[SignalKind]:
        return frozenset(kind for kind in SignalKind if self.has(kind))


@dataclass(frozen=True)
class OracleSignals:
    """The signal classes an oracle expects to see before a repair."""

    required: frozenset[SignalKind] = frozenset(SignalKind)

    def missing(self, evidence: Evidence) -> tuple[SignalKind, ...]:
        return tuple(
            kind
            for kind in SignalKind
            if kind in self.required and not evidence.has(kind)
        )


@dataclass(frozen=True)
class Diagnosis:
    """Anchored diagnosis produced from evidence."""

    main_cause: str
    failure_anchor: str
    behavior_error: str
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class Guidance:
    """Bounded recovery guidance."""

    text: str
    actions: tuple[str, ...]
    target_signals: tuple[SignalKind, ...]


@dataclass(frozen=True)
class RecoveryReport:
    """The complete PROBE recovery packet."""

    evidence: Evidence
    gaps: tuple[SignalKind, ...]
    diagnosis: Diagnosis
    guidance: Guidance
    grounded: bool

    @property
    def recoverable(self) -> bool:
        return self.grounded and bool(self.guidance.actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "gaps": [kind.value for kind in self.gaps],
            "diagnosis": {
                "main_cause": self.diagnosis.main_cause,
                "failure_anchor": self.diagnosis.failure_anchor,
                "behavior_error": self.diagnosis.behavior_error,
                "confidence": self.diagnosis.confidence,
            },
            "guidance": {
                "text": self.guidance.text,
                "actions": list(self.guidance.actions),
                "target_signals": [kind.value for kind in self.guidance.target_signals],
            },
            "grounded": self.grounded,
        }


class GuidanceGate:
    """Validate that guidance is grounded, actionable and scoped."""

    @staticmethod
    def verify(diagnosis: Diagnosis, evidence: Evidence) -> bool:
        if not diagnosis.main_cause.strip():
            return False
        if not diagnosis.failure_anchor.strip():
            return False
        if not diagnosis.behavior_error.strip():
            return False
        if not 0.0 <= diagnosis.confidence <= 1.0:
            return False
        if not evidence.kinds:
            return False
        return diagnosis.confidence >= 0.3

    @staticmethod
    def scope(guidance: Guidance, allowed: frozenset[SignalKind] | None = None) -> bool:
        if not guidance.actions or len(guidance.actions) > 6:
            return False
        if not guidance.text.strip():
            return False
        if allowed is not None and any(kind not in allowed for kind in guidance.target_signals):
            return False
        return True


class ProbeRecoveryEngine:
    """Extract evidence, diagnose, gate and bound the next attempt's guidance."""

    def __init__(self, max_actions: int = 3) -> None:
        if max_actions <= 0:
            raise ValueError("max_actions must be positive")
        self.max_actions = max_actions
        self.gate = GuidanceGate()

    def extract(self, telemetry: Telemetry) -> Evidence:
        reproduction = tuple(telemetry.test_names)
        regression = tuple(
            name for name in telemetry.test_names if telemetry.touched_files
        )
        location = tuple(telemetry.touched_files)
        context = tuple(telemetry.context_notes)
        api_usage = tuple(telemetry.api_calls)
        return Evidence(
            reproduction=reproduction,
            regression=regression,
            location=location,
            context=context,
            api_usage=api_usage,
        )

    def diagnose(self, telemetry: Telemetry, evidence: Evidence) -> Diagnosis:
        stderr_lines = [line.strip() for line in telemetry.stderr.splitlines() if line.strip()]
        main_cause = stderr_lines[-1] if stderr_lines else "unclassified failure"
        failure_anchor = (
            telemetry.touched_files[0]
            if telemetry.touched_files
            else (telemetry.command or telemetry.tool)
        )
        if not evidence.reproduction:
            behavior_error = "no reproduction test was observed"
        elif not evidence.regression:
            behavior_error = "reproduction was observed without a regression check"
        else:
            behavior_error = "observed behavior diverged from the expected contract"
        signals = len(evidence.kinds)
        confidence = 0.2 if signals == 0 else (0.5 if signals < 3 else 0.8)
        return Diagnosis(
            main_cause=main_cause,
            failure_anchor=failure_anchor,
            behavior_error=behavior_error,
            confidence=confidence,
        )

    def guide(self, diagnosis: Diagnosis, gaps: tuple[SignalKind, ...]) -> Guidance:
        actions: list[str] = []
        for kind in gaps:
            if kind is SignalKind.REPRODUCTION:
                actions.append("add a minimal failing reproduction test")
            elif kind is SignalKind.REGRESSION:
                actions.append("run the existing regression suite against the base commit")
            elif kind is SignalKind.LOCATION:
                actions.append("record the exact file and symbol where behavior diverges")
            elif kind is SignalKind.CONTEXT:
                actions.append("capture the call chain and relevant configuration")
            elif kind is SignalKind.API_USAGE:
                actions.append("record the API call shape and its observed response")
        actions = actions[: self.max_actions]
        text = f"Next attempt: {diagnosis.main_cause}"
        if actions:
            text += "; " + "; ".join(actions)
        return Guidance(
            text=text,
            actions=tuple(actions),
            target_signals=tuple(gaps[: self.max_actions]),
        )

    def recover(
        self,
        telemetry: Telemetry,
        oracle: OracleSignals | None = None,
    ) -> RecoveryReport:
        oracle = oracle or OracleSignals()
        evidence = self.extract(telemetry)
        gaps = oracle.missing(evidence)
        diagnosis = self.diagnose(telemetry, evidence)
        grounded = self.gate.verify(diagnosis, evidence)
        guidance = (
            self.guide(diagnosis, gaps)
            if grounded and gaps
            else Guidance(text="", actions=(), target_signals=())
        )
        if not self.gate.scope(guidance, oracle.required) and grounded and gaps:
            guidance = self.guide(diagnosis, gaps[:1])
        return RecoveryReport(
            evidence=evidence,
            gaps=gaps,
            diagnosis=diagnosis,
            guidance=guidance,
            grounded=grounded,
        )
