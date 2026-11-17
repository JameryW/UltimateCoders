"""Tests for the observed test-execution producer (#17/#21 data path)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from ultimate_coders.acceptance.test_execution import (
    TestDelta,
    TestExecutionError,
    TestRun,
    parse_junit_xml,
    run_test_delta,
    run_test_run,
)
from ultimate_coders.acceptance.test_execution import (
    test_execution_enabled as execution_enabled,
)


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stderr


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "acceptance@example.invalid")
    _git(repo, "config", "user.name", "Acceptance Test")
    test_file = repo / "test_sample.py"
    test_file.write_text(
        "def test_ok():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "def test_bug():\n"
        "    assert 1 == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "test_sample.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, test_file


def test_execution_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_TEST_EXECUTION", raising=False)
    assert execution_enabled() is False
    monkeypatch.setenv("UC_TEST_EXECUTION", "yes")
    assert execution_enabled() is True


def test_parse_junit_xml_records_worst_outcome(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuite><testcase classname="test_mod" name="test_pass" />'
        '<testcase classname="test_mod" name="test_fail"><failure /></testcase>'
        '<testcase name="test_skip"><skipped /></testcase>'
        '<testcase name="test_fail"><failure /></testcase>'
        '<testcase name="test_fail" /></testsuite>',
        encoding="utf-8",
    )
    cases = parse_junit_xml(report)
    assert cases["test_mod::test_pass"] == "passed"
    assert cases["test_mod::test_fail"] == "failed"
    assert cases["test_skip"] == "skipped"
    assert cases["test_fail"] == "failed"


def test_delta_rates_use_common_tests_only() -> None:
    base = TestRun(
        cases={
            "f2p": "failed",
            "still": "failed",
            "p2p": "passed",
            "broken": "passed",
            "skip": "skipped",
        },
        returncode=1,
        duration_seconds=0.1,
    )
    patched = TestRun(
        cases={
            "f2p": "passed",
            "still": "failed",
            "p2p": "passed",
            "broken": "failed",
            "skip": "skipped",
        },
        returncode=1,
        duration_seconds=0.1,
    )
    delta = TestDelta(
        base=base,
        patched=patched,
        common_tests=("broken", "f2p", "p2p", "skip", "still"),
        f2p_tests=("f2p",),
        p2p_tests=("p2p",),
        broken_tests=("broken",),
    )
    assert delta.f2p_rate == 0.5
    assert delta.p2p_rate == 0.5
    assert delta.pass_rate == 0.5
    assert delta.to_dict()["broken"] == ["broken"]


def test_non_pytest_command_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TestExecutionError, match="not a pytest invocation"):
        run_test_run(tmp_path, [sys.executable, "-c", "print('hi')"])


def test_real_repo_delta_detects_f2p_and_p2p(tmp_path: Path) -> None:
    repo, test_file = _make_repo(tmp_path)
    test_file.write_text(
        "def test_ok():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "def test_bug():\n"
        "    assert 1 == 1\n",
        encoding="utf-8",
    )
    delta = run_test_delta(repo, [sys.executable, "-m", "pytest", "-q"], timeout_seconds=60)
    assert delta.base.cases["test_sample::test_bug"] == "failed"
    assert delta.patched.cases["test_sample::test_bug"] == "passed"
    assert delta.f2p_tests == ("test_sample::test_bug",)
    assert delta.p2p_tests == ("test_sample::test_ok",)
    assert delta.broken_tests == ()
    assert delta.f2p_rate == 1.0
    assert delta.p2p_rate == 1.0
    assert delta.pass_rate == 1.0


def test_missing_head_snapshot_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TestExecutionError, match="git worktree root"):
        run_test_delta(empty, [sys.executable, "-m", "pytest", "-q"])
