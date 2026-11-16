"""Tests for the deterministic v10 anti-pattern acceptance gate."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.anti_pattern_gate import (
    AntiPatternFinding,
    AntiPatternReport,
    anti_pattern_gate_enabled,
    scan_anti_patterns,
)


def _clean_patch() -> tuple[dict[str, str], dict[str, str]]:
    before: dict[str, str] = {}
    after = {
        "pkg/calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": (
            "from pkg.calc import add\n\n\ndef test_add():\n"
            "    assert add(1, 2) == 3\n"
        ),
    }
    return before, after


def test_clean_patch_passes() -> None:
    before, after = _clean_patch()

    report = scan_anti_patterns(
        before,
        after,
        run_commands=("python -m pytest tests/test_calc.py",),
    )

    assert isinstance(report, AntiPatternReport)
    assert report.passed is True
    assert report.findings == ()
    assert report.errors == ()
    assert report.to_dict()["passed"] is True


def test_no_test_after_final_edit() -> None:
    before = {"pkg/calc.py": "def add(a, b):\n    return a + b\n"}
    after = {"pkg/calc.py": "def add(a, b):\n    return a + b + 1\n"}

    report = scan_anti_patterns(
        before,
        after,
        run_commands=("pytest tests/test_calc.py",),
        final_edit_after_tests=True,
    )

    assert any(item.kind == "no_test_after_final_edit" for item in report.findings)
    assert report.passed is False


def test_test_file_tampering_with_skip_marker() -> None:
    before = {
        "tests/test_calc.py": (
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
            "\n"
            "def test_sub():\n"
            "    assert sub(3, 1) == 2\n"
        )
    }
    after = {
        "tests/test_calc.py": (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
        )
    }

    report = scan_anti_patterns(
        before,
        after,
        run_commands=("pytest tests/test_calc.py",),
    )

    assert any(item.kind == "test_file_tampering" for item in report.findings)
    assert report.passed is False


def test_test_file_tampering_when_deleted() -> None:
    before = {"tests/test_calc.py": "def test_add():\n    assert True\n"}
    after: dict[str, str] = {}

    report = scan_anti_patterns(before, after, run_commands=("pytest",))

    assert any(item.kind == "test_file_tampering" for item in report.findings)


def test_authored_test_never_run() -> None:
    before: dict[str, str] = {}
    after = {"tests/test_new.py": "def test_new():\n    assert 1 + 1 == 2\n"}

    report = scan_anti_patterns(before, after)

    findings = [item for item in report.findings if item.kind == "authored_test_never_run"]
    assert findings
    assert findings[0].path == "tests/test_new.py"
    assert report.passed is False


def test_internal_symbol_leak() -> None:
    before: dict[str, str] = {}
    after = {
        "pkg/consumer.py": (
            "from pkg._internal import helper\n\n\ndef use():\n"
            "    return helper()\n"
        )
    }

    report = scan_anti_patterns(before, after, run_commands=("pytest",))

    assert any(item.kind == "internal_symbol_leak" for item in report.findings)


def test_report_errors_and_to_dict() -> None:
    finding = AntiPatternFinding(
        kind="internal_symbol_leak",
        severity="error",
        path="pkg/x.py",
        line=3,
        detail="example",
    )
    report = AntiPatternReport(findings=(finding,), passed=False, summary="one finding")

    assert report.errors == (finding,)
    assert report.to_dict()["findings"][0]["kind"] == "internal_symbol_leak"


def test_max_findings_is_respected() -> None:
    before: dict[str, str] = {}
    after = {f"pkg/mod_{index}.py": f"value = {index}\n" for index in range(5)}

    report = scan_anti_patterns(
        before,
        after,
        run_commands=("pytest",),
        final_edit_after_tests=True,
        max_findings=2,
    )

    assert len(report.findings) == 2
    assert report.passed is False


def test_anti_pattern_gate_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_ANTI_PATTERN_GATE", raising=False)
    assert anti_pattern_gate_enabled() is False
    monkeypatch.setenv("UC_ANTI_PATTERN_GATE", "yes")
    assert anti_pattern_gate_enabled() is True
    monkeypatch.setenv("UC_ANTI_PATTERN_GATE", "0")
    assert anti_pattern_gate_enabled() is False
