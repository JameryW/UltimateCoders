"""Real test execution data for acceptance and regression gates.

The V10 acceptance modules deliberately separated *declared* data from
*observed* data: ``TestGenNode`` accepts a candidate test artifact, ``GDPOGroup``
normalises scores, and ``CumulativeRegressionPlan`` consumes F2P/P2P
obligations.  This module is the missing producer for the observed half: it
runs a declared test command against the clean ``HEAD`` snapshot and the
working tree, parses JUnit XML, and reports which tests fail on the base and
pass after the change.

It never invents results.  A non-pytest command, missing JUnit report, or
timeout is an explicit :class:`TestExecutionError`; callers must treat that as
"no data", not as a pass.  The feature is gated by
``UC_TEST_EXECUTION`` so ordinary runs do not pay for an extra test run.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

__all__ = [
    "TestDelta",
    "TestExecutionError",
    "TestRun",
    "parse_junit_xml",
    "run_test_delta",
    "run_test_run",
    "test_execution_enabled",
]

TEST_EXECUTION_ENV = "UC_TEST_EXECUTION"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 900.0

_PASSED = "passed"
_FAILED = "failed"
_ERROR = "error"
_SKIPPED = "skipped"

_BAD_OUTCOMES = frozenset({_FAILED, _ERROR})
_GOOD_OUTCOMES = frozenset({_PASSED})


def test_execution_enabled() -> bool:
    """Feature flag: observed test execution (default off)."""

    raw = os.environ.get(TEST_EXECUTION_ENV)
    return raw is not None and raw.strip().lower() in {"1", "true", "yes", "on"}


class TestExecutionError(RuntimeError):
    """The declared test command could not produce trustworthy test data."""

    __test__ = False


@dataclass(frozen=True)
class TestRun:
    """One execution of a declared test command."""

    __test__ = False

    cases: Mapping[str, str]
    returncode: int
    duration_seconds: float
    command: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.cases.values() if outcome == _PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for outcome in self.cases.values() if outcome in _BAD_OUTCOMES)

    @property
    def skipped(self) -> int:
        return sum(1 for outcome in self.cases.values() if outcome == _SKIPPED)


@dataclass(frozen=True)
class TestDelta:
    """Base-vs-working-tree test delta used by acceptance/regression gates."""

    __test__ = False

    base: TestRun
    patched: TestRun
    common_tests: tuple[str, ...]
    f2p_tests: tuple[str, ...]
    p2p_tests: tuple[str, ...]
    broken_tests: tuple[str, ...]

    @property
    def pass_rate(self) -> float:
        total = sum(
            1 for outcome in self.patched.cases.values() if outcome != _SKIPPED
        )
        return self.patched.passed / total if total else 0.0

    @property
    def f2p_rate(self) -> float:
        base_failures = sum(
            1 for test_id in self.common_tests if self.base.cases[test_id] in _BAD_OUTCOMES
        )
        return len(self.f2p_tests) / base_failures if base_failures else 0.0

    @property
    def p2p_rate(self) -> float:
        base_passes = sum(
            1 for test_id in self.common_tests if self.base.cases[test_id] == _PASSED
        )
        return len(self.p2p_tests) / base_passes if base_passes else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_rate": round(self.pass_rate, 6),
            "f2p_rate": round(self.f2p_rate, 6),
            "p2p_rate": round(self.p2p_rate, 6),
            "base_tests": len(self.base.cases),
            "patched_tests": len(self.patched.cases),
            "f2p": list(self.f2p_tests),
            "p2p": list(self.p2p_tests),
            "broken": list(self.broken_tests),
        }


def parse_junit_xml(path: str | Path) -> dict[str, str]:
    """Parse a JUnit XML report into ``{test_id: outcome}``.

    ``test_id`` uses ``classname::name`` when a classname exists; duplicate
    ids keep the worst observed outcome so a rerun cannot hide a failure.
    """

    report = Path(path)
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise TestExecutionError(f"cannot read JUnit report {report}: {exc}") from exc

    cases: dict[str, str] = {}
    for case in root.iter("testcase"):
        classname = str(case.get("classname") or "").strip()
        name = str(case.get("name") or "").strip()
        test_id = f"{classname}::{name}" if classname and name else name or classname
        if not test_id:
            continue
        outcome = _PASSED
        if case.find("skipped") is not None:
            outcome = _SKIPPED
        elif case.find("failure") is not None:
            outcome = _FAILED
        elif case.find("error") is not None:
            outcome = _ERROR
        previous = cases.get(test_id)
        if previous is None or _outcome_rank(outcome) > _outcome_rank(previous):
            cases[test_id] = outcome
    return cases


def _outcome_rank(outcome: str) -> int:
    return {_PASSED: 0, _SKIPPED: 1, _ERROR: 2, _FAILED: 3}.get(outcome, 0)


def _command_parts(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command, posix=os.name != "nt")
    return [str(part) for part in command]


def _append_junit(argv: list[str], report: Path, rootdir: Path) -> list[str]:
    if any(part.startswith("--junitxml") or part.startswith("--junit-xml") for part in argv):
        return argv
    first = Path(argv[0]).name.lower() if argv else ""
    is_pytest = first in {"pytest", "py.test"}
    if not is_pytest and len(argv) >= 3:
        is_pytest = first.startswith("python") and argv[1:3] == ["-m", "pytest"]
    if not is_pytest:
        raise TestExecutionError(
            "test command is not a pytest invocation; per-test F2P/P2P data is unavailable"
        )
    augmented = [*argv, f"--junitxml={report}"]
    if not any(part.startswith("--rootdir") for part in argv):
        augmented.append(f"--rootdir={rootdir}")
    return augmented


def run_test_run(
    repo: str | Path,
    command: str | Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> TestRun:
    """Run a pytest command in ``repo`` and return its parsed results."""

    root = Path(repo)
    if not root.is_dir():
        raise TestExecutionError(f"test repository does not exist: {root}")
    timeout = max(1.0, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))
    argv = _command_parts(command)
    if not argv:
        raise TestExecutionError("test command is empty")

    with tempfile.TemporaryDirectory(prefix="uc-test-run-") as tmp:
        report = Path(tmp) / "junit.xml"
        full_argv = _append_junit(argv, report, root)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                full_argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TestExecutionError(
                f"test command failed to start or timed out: {type(exc).__name__}: {exc}"
            ) from exc
        duration = time.monotonic() - started
        if not report.is_file():
            raise TestExecutionError(
                "pytest produced no JUnit report; cannot derive per-test outcomes"
            )
        cases = parse_junit_xml(report)
        if not cases:
            raise TestExecutionError("pytest JUnit report contained no test cases")
        return TestRun(
            cases=cases,
            returncode=int(completed.returncode),
            duration_seconds=round(duration, 3),
            command=tuple(full_argv),
        )


def _assert_repo_root(repo: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TestExecutionError(
            f"cannot inspect git repository {repo}: {type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise TestExecutionError(f"not a git repository: {repo}")
    top = Path(result.stdout.strip()).resolve()
    if top != repo.resolve():
        raise TestExecutionError(f"test execution requires the git worktree root, got {top}")


def _extract_head(repo: Path, destination: Path) -> None:
    _assert_repo_root(repo)
    archive = destination.with_suffix(".zip")
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "archive",
                "--format=zip",
                "--output",
                str(archive),
                "HEAD",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TestExecutionError(
            f"git archive HEAD failed: {type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise TestExecutionError(f"git archive HEAD failed: {result.stderr.strip()}")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TestExecutionError(f"cannot extract HEAD snapshot: {exc}") from exc


def run_test_delta(
    repo: str | Path,
    command: str | Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> TestDelta:
    """Compare a test command on ``HEAD`` and on the current working tree."""

    root = Path(repo)
    with tempfile.TemporaryDirectory(prefix="uc-test-base-") as tmp:
        base_root = Path(tmp) / "base"
        _extract_head(root, base_root)
        base = run_test_run(base_root, command, timeout_seconds=timeout_seconds)
    patched = run_test_run(root, command, timeout_seconds=timeout_seconds)

    common = tuple(sorted(set(base.cases).intersection(patched.cases)))
    if not common:
        raise TestExecutionError("base and working tree share no test cases")
    f2p: list[str] = []
    p2p: list[str] = []
    broken: list[str] = []
    for test_id in common:
        before = base.cases[test_id]
        after = patched.cases[test_id]
        if before in _BAD_OUTCOMES and after in _GOOD_OUTCOMES:
            f2p.append(test_id)
        elif before in _GOOD_OUTCOMES and after in _GOOD_OUTCOMES:
            p2p.append(test_id)
        elif before in _GOOD_OUTCOMES and after in _BAD_OUTCOMES:
            broken.append(test_id)
    return TestDelta(
        base=base,
        patched=patched,
        common_tests=common,
        f2p_tests=tuple(f2p),
        p2p_tests=tuple(p2p),
        broken_tests=tuple(broken),
    )
