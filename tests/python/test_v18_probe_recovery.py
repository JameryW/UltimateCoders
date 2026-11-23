"""Tests for the V18 PROBE recovery engine (#70)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.probe_recovery import (
    Diagnosis,
    Evidence,
    Guidance,
    GuidanceGate,
    OracleSignals,
    ProbeRecoveryEngine,
    SignalKind,
    Telemetry,
    probe_recovery_enabled,
)


def telemetry() -> Telemetry:
    return Telemetry(
        tool="pytest",
        command="pytest -q tests/test_cache.py",
        stderr="AssertionError: expected 1, got 2",
        touched_files=("src/cache.py",),
        test_names=("tests/test_cache.py::test_get",),
        context_notes=("Cache.get is called by service.load",),
        api_calls=("Cache.get(key)",),
    )


def test_flag_defaults_off_and_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not probe_recovery_enabled()
    monkeypatch.setenv("UC_PROBE_RECOVERY", "true")
    assert probe_recovery_enabled()


def test_extract_populates_all_five_signals() -> None:
    evidence = ProbeRecoveryEngine().extract(telemetry())
    assert evidence.kinds == frozenset(SignalKind)
    assert evidence.reproduction == ("tests/test_cache.py::test_get",)
    assert evidence.location == ("src/cache.py",)


def test_missing_signals_are_reported_in_order() -> None:
    evidence = ProbeRecoveryEngine().extract(
        Telemetry(tool="pytest", touched_files=("src/cache.py",))
    )
    gaps = OracleSignals().missing(evidence)
    assert gaps == (
        SignalKind.REPRODUCTION,
        SignalKind.REGRESSION,
        SignalKind.CONTEXT,
        SignalKind.API_USAGE,
    )


def test_diagnosis_uses_last_stderr_line_and_anchor() -> None:
    diagnosis = ProbeRecoveryEngine().diagnose(
        telemetry(), ProbeRecoveryEngine().extract(telemetry())
    )
    assert diagnosis.main_cause == "AssertionError: expected 1, got 2"
    assert diagnosis.failure_anchor == "src/cache.py"
    assert 0.0 <= diagnosis.confidence <= 1.0


def test_gate_rejects_ungrounded_diagnosis() -> None:
    gate = GuidanceGate()
    assert not gate.verify(
        Diagnosis("", "src/cache.py", "mismatch", 0.8),
        Evidence(location=("src/cache.py",)),
    )
    assert not gate.verify(
        Diagnosis("cause", "src/cache.py", "mismatch", 0.1),
        Evidence(location=("src/cache.py",)),
    )
    assert not gate.verify(
        Diagnosis("cause", "src/cache.py", "mismatch", 0.8), Evidence()
    )


def test_guidance_is_bounded_and_targets_gaps() -> None:
    engine = ProbeRecoveryEngine(max_actions=2)
    diagnosis = Diagnosis("cause", "src/cache.py", "mismatch", 0.8)
    guidance = engine.guide(diagnosis, (SignalKind.REPRODUCTION, SignalKind.API_USAGE))
    assert len(guidance.actions) == 2
    assert guidance.target_signals == (SignalKind.REPRODUCTION, SignalKind.API_USAGE)
    assert "Next attempt" in guidance.text


def test_recover_releases_grounded_guidance() -> None:
    report = ProbeRecoveryEngine().recover(telemetry())
    assert report.grounded
    assert report.gaps == ()
    assert report.guidance.actions == ()
    assert report.recoverable is False


def test_recover_guides_missing_signals() -> None:
    partial = Telemetry(
        tool="pytest",
        stderr="boom",
        touched_files=("src/cache.py",),
    )
    report = ProbeRecoveryEngine().recover(partial)
    assert report.grounded
    assert SignalKind.REPRODUCTION in report.gaps
    assert report.guidance.actions


def test_guidance_scope_rejects_unknown_target() -> None:
    guidance = Guidance("text", ("act",), (SignalKind.REPRODUCTION,))
    assert GuidanceGate.scope(guidance, frozenset({SignalKind.API_USAGE})) is False
    assert GuidanceGate.scope(guidance, frozenset({SignalKind.REPRODUCTION})) is True


def test_report_to_dict_is_serialisable_shape() -> None:
    report = ProbeRecoveryEngine().recover(telemetry())
    data = report.to_dict()
    assert data["grounded"] is True
    assert data["gaps"] == []
    assert data["diagnosis"]["failure_anchor"] == "src/cache.py"
