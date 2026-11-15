"""Tests for deterministic process-level acceptance review."""

from __future__ import annotations

import pytest
from ultimate_coders.architecture.process_verifier import (
    CLEAN_SUMMARY,
    ProcessReviewRejected,
    apply_process_review,
    process_verifier_enabled,
    verify_process,
)


def test_clean_product_change_passes() -> None:
    before = {"python/mod.py": "def add(a, b):\n    return a + b\n"}
    after = {"python/mod.py": "def add(a, b):\n    return a + b  # improved\n"}

    report = verify_process(before, after)

    assert report.passed is True
    assert report.findings == ()
    assert report.errors == ()
    assert report.warnings == ()
    assert report.causal_summary == CLEAN_SUMMARY


def test_deleted_assertion_is_error_with_before_line() -> None:
    before = {"tests/test_math.py": "def test_add():\n    assert add(1, 2) == 3\n"}
    after = {"tests/test_math.py": "def test_add():\n    return None\n"}

    report = verify_process(before, after)

    assert report.passed is False
    assert len(report.errors) == 1
    finding = report.errors[0]
    assert finding.kind == "deleted_assertion"
    assert finding.severity == "error"
    assert finding.path == "tests/test_math.py"
    assert finding.line == 2
    assert "assert add" in finding.detail
    assert "tests/test_math.py:2: deleted_assertion —" in report.causal_summary


def test_replaced_assertion_is_not_reported() -> None:
    before = {"tests/test_math.py": "def test_add():\n    assert add(1, 2) == 3\n"}
    after = {"tests/test_math.py": "def test_add():\n    assert add(1, 2) == 4\n"}

    report = verify_process(before, after)

    assert report.passed is True
    assert [item for item in report.findings if item.kind == "deleted_assertion"] == []


def test_added_skip_marker_is_bypass_error() -> None:
    before = {"tests/test_flow.py": "def test_flow():\n    assert run()\n"}
    after = {
        "tests/test_flow.py": (
            'def test_flow():\n    pytest.skip("flaky")\n    assert run()\n'
        )
    }

    report = verify_process(before, after)

    bypass = [item for item in report.findings if item.kind == "test_bypass"]
    assert len(bypass) == 1
    assert bypass[0].severity == "error"
    assert bypass[0].line == 2
    assert report.passed is False


def test_added_mark_skip_and_xfail_are_bypass_errors() -> None:
    before = {"tests/test_flow.py": "def test_flow():\n    assert run()\n"}
    after = {
        "tests/test_flow.py": (
            "def test_flow():\n"
            '    pytest.mark.skip("later")\n'
            "    assert run()\n"
            "\n"
            "@pytest.mark.xfail\n"
            "def test_todo():\n"
            "    assert False\n"
        )
    }

    report = verify_process(before, after)

    bypass = [item for item in report.findings if item.kind == "test_bypass"]
    assert len(bypass) == 2
    assert {item.line for item in bypass} == {2, 5}


def test_bare_return_before_assertion_is_bypass_error() -> None:
    before = {
        "tests/test_flow.py": (
            "def test_flow(flag):\n    result = compute()\n    assert result\n"
        )
    }
    after = {
        "tests/test_flow.py": (
            "def test_flow(flag):\n"
            "    result = compute()\n"
            "    if flag:\n"
            "        return\n"
            "    assert result\n"
        )
    }

    report = verify_process(before, after)

    bypass = [item for item in report.findings if item.kind == "test_bypass"]
    assert len(bypass) == 1
    assert bypass[0].line == 4
    assert "bare return" in bypass[0].detail
    assert report.passed is False


def test_product_environment_guard_is_bypass_error() -> None:
    before = {"python/mod.py": "def handle(payload):\n    return payload\n"}
    after = {
        "python/mod.py": (
            "def handle(payload):\n"
            '    if os.environ.get("PYTEST_CURRENT_TEST"):\n'
            "        return\n"
            "    return payload\n"
        )
    }

    report = verify_process(before, after)

    assert report.passed is False
    assert len(report.errors) == 1
    finding = report.errors[0]
    assert finding.kind == "test_bypass"
    assert finding.line == 2
    assert "PYTEST_CURRENT_TEST" in finding.detail


def test_same_line_test_environment_exit_is_bypass_error() -> None:
    before = {"python/mod.py": "def handle(payload):\n    return payload\n"}
    after = {
        "python/mod.py": (
            "def handle(payload):\n"
            '    if os.environ.get("CI"): sys.exit(0)\n'
            "    return payload\n"
        )
    }

    report = verify_process(before, after)

    assert report.passed is False
    finding = report.errors[0]
    assert finding.kind == "test_bypass"
    assert "sys.exit(0)" in finding.detail


def test_exception_swallow_is_warning() -> None:
    before = {"python/mod.py": "def run():\n    return risky()\n"}
    after = {
        "python/mod.py": (
            "def run():\n"
            "    try:\n"
            "        return risky()\n"
            "    except Exception:\n"
            "        pass\n"
        )
    }

    report = verify_process(before, after)

    assert report.passed is True
    assert len(report.warnings) == 1
    finding = report.warnings[0]
    assert finding.kind == "exception_swallow"
    assert finding.severity == "warning"
    assert finding.line == 4
    assert "only 'pass'" in finding.detail


def test_exception_swallow_with_logging_is_not_reported() -> None:
    before = {"python/mod.py": "def run():\n    return risky()\n"}
    after = {
        "python/mod.py": (
            "def run():\n"
            "    try:\n"
            "        return risky()\n"
            "    except Exception:\n"
            '        LOGGER.exception("boom")\n'
            "        raise\n"
        )
    }

    report = verify_process(before, after)

    assert [item for item in report.findings if item.kind == "exception_swallow"] == []


def test_lucky_pass_for_added_test_without_assertion() -> None:
    before = {"tests/test_flow.py": "def test_old():\n    assert old()\n"}
    after = {
        "tests/test_flow.py": (
            "def test_old():\n"
            "    assert old()\n"
            "\n"
            "def test_new():\n"
            "    compute()\n"
        )
    }

    report = verify_process(before, after)

    assert report.passed is True
    lucky = [item for item in report.findings if item.kind == "lucky_pass"]
    assert len(lucky) == 2
    assert {item.severity for item in lucky} == {"warning"}
    assert any(item.line == 4 and "test_new" in item.detail for item in lucky)
    assert any("without adding an assertion" in item.detail for item in lucky)


def test_lucky_pass_for_deleted_test_functions_without_assertion_gain() -> None:
    before = {"tests/test_old.py": 'def test_gone():\n    print("x")\n'}
    after = {"tests/test_old.py": ""}

    report = verify_process(before, after)

    assert report.passed is True
    assert [item for item in report.findings if item.kind == "deleted_assertion"] == []
    assert [item for item in report.findings if item.kind == "lucky_pass"]


def test_exception_handler_near_miss_is_ignored() -> None:
    before = {"python/mod.py": "def run():\n    return risky()\n"}
    after = {
        "python/mod.py": (
            "def run():\n"
            "    try:\n"
            "        return risky()\n"
            "    except ValueError:\n"
            "        return None\n"
        )
    }

    report = verify_process(before, after)

    assert report.findings == ()


def test_causal_summary_has_at_most_eight_error_lines() -> None:
    before = {}
    after = {}
    for index in range(10):
        path = f"tests/test_mod_{index}.py"
        before[path] = f"def test_{index}():\n    assert value_{index}\n"
        after[path] = f"def test_{index}():\n    print('tampered')\n"

    report = verify_process(before, after)

    assert report.passed is False
    assert len(report.errors) == 10
    lines = report.causal_summary.splitlines()
    assert len(lines) == 8
    assert lines[0].startswith("tests/test_mod_0.py:2: deleted_assertion —")


def test_max_findings_truncates_but_passed_uses_all_findings() -> None:
    before = {
        "tests/test_a.py": "def test_a():\n    assert a()\n",
        "tests/test_b.py": "def test_b():\n    assert b()\n",
    }
    after = {
        "tests/test_a.py": "def test_a():\n    print('a')\n",
        "tests/test_b.py": "def test_b():\n    print('b')\n",
    }

    report = verify_process(before, after, max_findings=1)

    assert len(report.findings) == 1
    assert report.findings[0].path == "tests/test_a.py"
    assert report.passed is False
    assert len(report.causal_summary.splitlines()) == 1


def test_explicit_test_paths_extend_automatic_detection() -> None:
    before = {"spec/checks.py": "def check():\n    assert run()\n"}
    after = {"spec/checks.py": "def check():\n    return None\n"}

    default_report = verify_process(before, after)
    explicit_report = verify_process(before, after, test_paths=("spec/checks.py",))

    assert default_report.passed is True
    assert explicit_report.passed is False
    assert explicit_report.errors[0].kind == "deleted_assertion"


def test_max_findings_must_be_positive() -> None:
    with pytest.raises(ValueError):
        verify_process({}, {}, max_findings=0)


def test_report_to_dict_round_trip() -> None:
    report = verify_process(
        {"tests/test_math.py": "def test_add():\n    assert add(1, 2) == 3\n"},
        {"tests/test_math.py": "def test_add():\n    pass\n"},
    )

    payload = report.to_dict()

    assert set(payload) == {"findings", "passed", "causal_summary"}
    assert payload["passed"] is False
    assert payload["findings"][0] == {
        "kind": "deleted_assertion",
        "severity": "error",
        "path": "tests/test_math.py",
        "line": 2,
        "detail": report.findings[0].detail,
    }


def test_apply_process_review_raises_when_enforced(monkeypatch) -> None:
    report = verify_process(
        {"tests/test_math.py": "def test_add():\n    assert add(1, 2) == 3\n"},
        {"tests/test_math.py": "def test_add():\n    pass\n"},
    )
    monkeypatch.setenv("UC_PROCESS_VERIFIER_ENFORCE", "1")

    with pytest.raises(ProcessReviewRejected) as excinfo:
        apply_process_review(report)

    assert excinfo.value.report is report
    assert "deleted_assertion" in str(excinfo.value)


def test_apply_process_review_is_noop_when_disabled_or_non_strict(monkeypatch) -> None:
    report = verify_process(
        {"tests/test_math.py": "def test_add():\n    assert add(1, 2) == 3\n"},
        {"tests/test_math.py": "def test_add():\n    pass\n"},
    )
    monkeypatch.delenv("UC_PROCESS_VERIFIER_ENFORCE", raising=False)

    assert apply_process_review(report) is None

    monkeypatch.setenv("UC_PROCESS_VERIFIER_ENFORCE", "true")
    assert apply_process_review(report, strict=False) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_process_verifier_enabled_flag(monkeypatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("UC_PROCESS_VERIFIER_ENFORCE", value)

    assert process_verifier_enabled() is expected


def test_process_verifier_enabled_without_env(monkeypatch) -> None:
    monkeypatch.delenv("UC_PROCESS_VERIFIER_ENFORCE", raising=False)

    assert process_verifier_enabled() is False
