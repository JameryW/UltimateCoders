"""Tests for the v10 SWE-Doctor runtime diagnosis parser (#22)."""

from __future__ import annotations

from ultimate_coders.acceptance.swe_doctor_diag import (
    ExpectedBehaviour,
    RuntimeDiagnosisReport,
    VariableSnapshot,
    diagnose_runtime,
    swe_doctor_enabled,
)

SAMPLE_TRACEBACK = """\
_____________________________ test_calculate ______________________________
test_probe.py:17: in test_calculate
    assert result == expected
E   assert 3 == 4
        calculator = <test_probe.Calculator object at 0x000001>
        expected   = 4
        result     = 3
        x          = 1
        y          = 2
_____________________________ test_other __________________________________
test_probe.py:30: in test_other
    assert value > 0
E   assert -1 > 0
        value      = -1
"""


def test_parses_sample_traceback() -> None:
    report = diagnose_runtime(
        SAMPLE_TRACEBACK,
        expected=["Calculator.add returns the sum of its inputs"],
    )
    assert isinstance(report, RuntimeDiagnosisReport)
    assert isinstance(report.expected[0], ExpectedBehaviour)
    assert report.expected[0].source == "caller"
    assert "result == expected" in report.expected_clauses
    assert isinstance(report.snapshots[0], VariableSnapshot)
    names = [snapshot.name for snapshot in report.snapshots]
    assert names == ["calculator", "expected", "result", "x", "y", "value"]
    assert report.snapshots[2].value == "3"
    assert report.snapshots[2].frame == "test_probe.py:17: in test_calculate"
    assert report.divergence_frame == "test_probe.py:17: in test_calculate"
    assert "test_calculate" in report.summary
    assert "Expected: Calculator.add returns" in report.summary


def test_snapshot_cap() -> None:
    report = diagnose_runtime(SAMPLE_TRACEBACK, max_snapshots=2)
    assert len(report.snapshots) == 2
    assert [snapshot.name for snapshot in report.snapshots] == ["calculator", "expected"]


def test_missing_data_is_safe() -> None:
    report = diagnose_runtime("", expected=("a behavior",))
    assert report.snapshots == ()
    assert report.divergence_frame == ""
    assert report.expected_clauses == ("a behavior",)
    assert "No runtime failure frames" in report.summary
    assert diagnose_runtime(None).snapshots == ()


def test_report_serialises() -> None:
    payload = diagnose_runtime(SAMPLE_TRACEBACK, max_snapshots=1).to_dict()
    assert payload["snapshots"] == [
        {
            "name": "calculator",
            "value": "<test_probe.Calculator object at 0x000001>",
            "frame": "test_probe.py:17: in test_calculate",
        }
    ]
    assert payload["expected"][0]["source"] == "test_probe.py:17"


def test_accepts_expected_behaviour_objects() -> None:
    report = diagnose_runtime(
        SAMPLE_TRACEBACK,
        expected=[ExpectedBehaviour(clause="sum is four", source="issue-42")],
        max_snapshots=0,
    )
    assert report.expected[0].clause == "sum is four"
    assert report.expected[0].source == "issue-42"
    assert report.snapshots == ()


def test_flag(monkeypatch) -> None:
    monkeypatch.delenv("UC_SWE_DOCTOR", raising=False)
    assert swe_doctor_enabled() is False
    monkeypatch.setenv("UC_SWE_DOCTOR", "on")
    assert swe_doctor_enabled() is True
    monkeypatch.setenv("UC_SWE_DOCTOR", "false")
    assert swe_doctor_enabled() is False
