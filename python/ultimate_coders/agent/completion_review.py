"""Completion review gate for the local harness (v30 wiring of V13 #40).

The V13 #40 upgrade shipped an L3 isolated reviewer
(:mod:`ultimate_coders.acceptance.independent_reviewer`) that judges a patch
without any trajectory context, but nothing in the repository called it: the
local harness accepted ``done()`` as soon as the loop's own gates were quiet,
so a patch that edited the tests, deleted an assertion or carried a
``--no-verify`` marker completed exactly like a verified one.  Ledger #40's
measurement is the reason this matters -- functionally passing patches still
violate review constraints often enough that an isolated critic is worth its
cost.

This module is the missing caller.  At ``done()`` it feeds the reviewer

* the **real workspace diff** (``git diff`` against the run's base commit, so
  only this run's edits are judged), and
* the run's **own test evidence** (which test-looking commands ran and how they
  exited),

and turns the verdict into either a completion block or an advisory note.

Blocking is deliberately narrow.  The reviewer's hard findings are
unambiguous evidence of an unverified or tampered patch -- test-file edits,
deleted assertions, verification-bypass markers, protected-path edits and
failed test runs -- so those block completion.  *Missing* test evidence is only
advisory: the local harness cannot know whether a task even has tests, and
blocking on it would livelock documentation or configuration work.  An empty
diff is skipped for the same reason.

Everything here is deterministic bookkeeping: no network, no model calls.

Flag: ``UC_COMPLETION_REVIEW`` (default off).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ultimate_coders.acceptance.independent_reviewer import (
    Finding,
    IndependentReviewer,
    ReviewVerdict,
    TestReport,
    changed_paths,
)

__all__ = [
    "ADVISORY_CODES",
    "COMPLETION_REVIEW_ENABLED_ENV",
    "CompletionReview",
    "CompletionReviewGate",
    "MAX_PATCH_CHARS",
    "TEST_COMMAND_HINTS",
    "TestRun",
    "build_test_report",
    "completion_review_enabled",
    "describe_decision",
    "docs_only_paths",
    "looks_like_test_command",
    "workspace_diff",
]

COMPLETION_REVIEW_ENABLED_ENV = "UC_COMPLETION_REVIEW"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Commands whose exit code counts as verification evidence.
TEST_COMMAND_HINTS: tuple[str, ...] = (
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
    "make test",
    "tox",
    "nox",
    "jest",
    "vitest",
    "rspec",
    "phpunit",
    "mvn test",
    "gradle test",
)

#: Findings that are recorded but never block completion.
ADVISORY_CODES = frozenset(
    {
        "empty_patch",
        "no_test_evidence",
        "tests_not_executed",
    }
)

#: Upper bound on the diff handed to the reviewer, in characters.
MAX_PATCH_CHARS = 400_000

_GIT_TIMEOUT_S = 10.0
_DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt", ".adoc")
_DOC_PREFIXES = ("docs/", "doc/")
_MAX_NEW_FILE_CHARS = 20_000
_MAX_NEW_FILE_LINES = 400


def completion_review_enabled() -> bool:
    """Whether the completion review gate is on for this call."""

    return os.environ.get(COMPLETION_REVIEW_ENABLED_ENV, "").strip().lower() in _TRUTHY


def looks_like_test_command(command: str) -> bool:
    """Whether a shell command counts as test evidence."""

    lowered = str(command or "").strip().lower()
    if not lowered:
        return False
    return any(hint in lowered for hint in TEST_COMMAND_HINTS)


@dataclass(frozen=True)
class TestRun:
    """One observed command execution that counts as verification evidence."""

    #: Keep pytest from collecting this bookkeeping record as a test class.
    __test__ = False

    command: str
    exit_code: int

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, object]:
        return {"command": self.command, "exit_code": self.exit_code}


def build_test_report(runs: Sequence[TestRun]) -> TestReport | None:
    """Build the reviewer's test report from observed command exits."""

    if not runs:
        return None
    passed = sum(1 for run in runs if run.passed)
    failed = len(runs) - passed
    commands = "; ".join(run.command for run in runs[:4])
    return TestReport(
        passed=passed,
        failed=failed,
        total=len(runs),
        summary=commands[:200],
    )


def docs_only_paths(paths: Iterable[str]) -> bool:
    """Whether every changed path is documentation."""

    entries = [str(path) for path in paths if str(path).strip()]
    if not entries:
        return False
    for path in entries:
        lowered = path.lower()
        if lowered.startswith(_DOC_PREFIXES) or lowered.endswith(_DOC_SUFFIXES):
            continue
        return False
    return True


def _git_output(root: Path | str, *args: str) -> str:
    """Run one fixed-argv git command and return stdout, or ``""`` on failure."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _is_git_root(root: Path | str) -> bool:
    """Whether ``root`` is the top level of its own repository.

    A pytest ``tmp_path`` lives inside this checkout, so an unguarded
    ``git -C <tmp>`` would silently diff the *outer* repository.  The harness
    only ever reviews the run's own working directory, so a nested directory is
    treated as "no repository".
    """

    toplevel = _git_output(root, "rev-parse", "--show-toplevel").strip()
    if not toplevel:
        return False
    inner = os.path.normcase(Path(toplevel).resolve().as_posix())
    outer = os.path.normcase(Path(str(root)).resolve().as_posix())
    return inner == outer


def _untracked_hunk(root: Path | str, rel: str) -> str:
    """Synthesize a ``git diff`` hunk for a file this run created.

    ``git diff`` never shows untracked files, so a completion that only adds new
    files would look like an empty patch.  The harness tells us which paths it
    wrote, and those are rendered here as new-file hunks without touching the
    repository index (``git add -N`` would mutate the caller's index).
    """

    path = Path(str(root)) / rel
    try:
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    if "\x00" in content:
        return ""
    lines = content[:_MAX_NEW_FILE_CHARS].splitlines()[:_MAX_NEW_FILE_LINES]
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{rel} b/{rel}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{rel}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


def workspace_diff(
    root: Path | str,
    *,
    base_commit: str = "",
    written_paths: Iterable[str] = (),
) -> str:
    """Return the workspace diff this run produced, or ``""`` when unknown.

    ``base_commit`` pins the comparison to the commit the run started from, so
    a dirty checkout inherited from an earlier attempt is not re-reviewed.
    ``written_paths`` are the task-relative files the harness itself created;
    they are rendered as new-file hunks because ``git diff`` omits untracked
    files.  Every git invocation is fixed-argv and never reaches a shell.
    """

    if not _is_git_root(root):
        return ""
    args = ["diff", "--no-color", "--no-ext-diff", "--unified=3"]
    if base_commit:
        args.append(base_commit)
    patch = _git_output(root, *args)[:MAX_PATCH_CHARS]
    seen = {str(path) for path in written_paths if str(path).strip()}
    if seen:
        extra: list[str] = []
        for rel in sorted(seen):
            if f"+++ b/{rel}" in patch:
                continue
            hunk = _untracked_hunk(root, rel)
            if hunk:
                extra.append(hunk)
        if extra:
            patch = (patch + "".join(extra))[:MAX_PATCH_CHARS]
    return patch


@dataclass(frozen=True)
class CompletionReview:
    """The gate's decision for one ``done()`` attempt."""

    verdict: ReviewVerdict | None
    blocked: bool
    findings: tuple[Finding, ...] = ()
    advisory: tuple[Finding, ...] = ()
    skipped: str = ""
    test_runs: int = 0

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "blocked": self.blocked,
            "tests_run": self.test_runs,
        }
        if self.skipped:
            payload["skipped"] = self.skipped
        if self.verdict is not None:
            payload["decision"] = self.verdict.decision.value
            payload["reviewed_paths"] = list(self.verdict.reviewed_paths)
            payload["confidence"] = self.verdict.confidence
        if self.findings:
            payload["findings"] = [finding.to_dict() for finding in self.findings]
        if self.advisory:
            payload["advisory"] = [finding.to_dict() for finding in self.advisory]
        return payload

    def to_tool_error(self) -> str:
        """The tool result that sends the completion back to the agent."""

        targets = sorted({finding.path for finding in self.findings if finding.path})
        hint = "Fix the review findings, then call done() again."
        if targets:
            hint = (
                "The patch cannot be accepted as-is: fix the review findings for "
                f"{', '.join(targets[:5])}, re-run the tests, then call done() again."
            )
        return json.dumps(
            {
                "error": "completion_review_blocked",
                "hint": hint,
                "findings": [finding.to_dict() for finding in self.findings],
                "blocked_once": True,
            },
            ensure_ascii=False,
        )


@dataclass
class CompletionReviewGate:
    """Run the L3 reviewer against a finishing attempt, at most ``max_blocks``."""

    max_blocks: int = 1
    reviewer: IndependentReviewer = field(default_factory=IndependentReviewer)
    blocks: int = 0
    last: CompletionReview | None = None

    def __post_init__(self) -> None:
        if self.max_blocks < 0:
            raise ValueError("max_blocks must be non-negative")

    def review(
        self,
        root: Path | str,
        *,
        base_commit: str = "",
        test_runs: Sequence[TestRun] = (),
        written_paths: Iterable[str] = (),
    ) -> CompletionReview:
        """Judge the workspace diff plus this run's test evidence."""

        patch = workspace_diff(
            root,
            base_commit=base_commit,
            written_paths=written_paths,
        )
        paths = changed_paths(patch)
        if not patch.strip():
            result = CompletionReview(
                verdict=None,
                blocked=False,
                skipped="no_diff",
                test_runs=len(test_runs),
            )
            self.last = result
            return result

        report = build_test_report(test_runs)
        verdict = self.reviewer.review(
            patch,
            test_report=report,
            docs_only=docs_only_paths(paths),
        )
        hard = tuple(
            finding
            for finding in verdict.findings
            if finding.code not in ADVISORY_CODES
        )
        advisory = tuple(
            finding for finding in verdict.findings if finding.code in ADVISORY_CODES
        )
        blocked = bool(hard) and self.blocks < self.max_blocks
        if blocked:
            self.blocks += 1
        result = CompletionReview(
            verdict=verdict,
            blocked=blocked,
            findings=hard,
            advisory=advisory,
            test_runs=len(test_runs),
        )
        self.last = result
        return result

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"blocks": self.blocks}
        if self.last is not None:
            payload["last"] = self.last.to_dict()
        return payload


def describe_decision(verdict: ReviewVerdict | None) -> str:
    """Small helper for emit payloads and tests."""

    return "skipped" if verdict is None else verdict.decision.value
