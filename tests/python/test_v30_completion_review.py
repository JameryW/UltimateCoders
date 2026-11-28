"""Tests for the completion review gate (V13 #40 wiring in the local harness).

The gate hands the L3 isolated reviewer the diff a run produced plus the exit
codes of test-looking commands, and blocks ``done()`` on hard findings (test
files edited, assertions deleted, bypass markers, failed tests).  Missing test
evidence is advisory so documentation and configuration tasks can still finish.

These tests cover the helpers, the gate's block/advisory split, and the harness
wiring itself.  The reviewer's own rules are covered by
``test_v13_independent_reviewer.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from ultimate_coders.agent import completion_review as cr
from ultimate_coders.agent import harness_local_loop as hll
from ultimate_coders.agent.completion_review import (
    ADVISORY_CODES,
    COMPLETION_REVIEW_ENABLED_ENV,
    CompletionReviewGate,
    TestRun,
    build_test_report,
    completion_review_enabled,
    describe_decision,
    docs_only_paths,
    looks_like_test_command,
    workspace_diff,
)


def _resp(tool_calls: list[tuple[str, dict]], text: str = "") -> SimpleNamespace:
    calls = [
        SimpleNamespace(
            id=f"c{i}",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for i, (name, args) in enumerate(tool_calls)
    ]
    choice = SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    return SimpleNamespace(choices=[choice], usage=usage)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _head(root: Path) -> str:
    return subprocess.run(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestFlag:
    def test_flag_reads_truthy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(COMPLETION_REVIEW_ENABLED_ENV, raising=False)
        assert completion_review_enabled() is False
        for value in ("1", "true", "YES", " on "):
            monkeypatch.setenv(COMPLETION_REVIEW_ENABLED_ENV, value)
            assert completion_review_enabled() is True, value
        for value in ("0", "false", "", "nope"):
            monkeypatch.setenv(COMPLETION_REVIEW_ENABLED_ENV, value)
            assert completion_review_enabled() is False, value


class TestHelpers:
    @pytest.mark.parametrize(
        "command",
        [
            "pytest -q",
            "python -m pytest tests/python",
            "cargo test -p uc-engine",
            "bun test src/rl",
            "npm test",
            "go test ./...",
        ],
    )
    def test_test_commands_are_detected(self, command: str) -> None:
        assert looks_like_test_command(command) is True

    @pytest.mark.parametrize("command", ["", "ls -la", "echo hi", "git status"])
    def test_other_commands_are_not_evidence(self, command: str) -> None:
        assert looks_like_test_command(command) is False

    def test_test_report_is_none_without_runs(self) -> None:
        assert build_test_report([]) is None

    def test_test_report_counts_exit_codes(self) -> None:
        report = build_test_report(
            [
                TestRun("pytest -q", 0),
                TestRun("pytest tests/x", 1),
            ]
        )
        assert report is not None
        assert (report.passed, report.failed, report.total) == (1, 1, 2)
        assert "pytest -q" in (report.summary or "")

    def test_test_run_passed_property(self) -> None:
        assert TestRun("pytest", 0).passed is True
        assert TestRun("pytest", 3).passed is False

    def test_docs_only_paths(self) -> None:
        assert docs_only_paths(["README.md", "docs/arch.md"]) is True
        assert docs_only_paths(["app.py"]) is False
        assert docs_only_paths(["docs/x.md", "app.py"]) is False
        assert docs_only_paths([]) is False

    def test_describe_decision(self) -> None:
        assert describe_decision(None) == "skipped"


class TestWorkspaceDiff:
    def test_returns_the_edit(self, repo: Path) -> None:
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        patch = workspace_diff(repo)
        assert "+++ b/app.py" in patch
        assert "+print('v2')" in patch

    def test_pinned_base_still_shows_the_runs_committed_change(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "run")
        assert "+++ b/app.py" in workspace_diff(repo, base_commit=base)
        # Without a pin the same change is invisible: it is already committed.
        assert workspace_diff(repo) == ""

    def test_new_files_are_rendered_from_written_paths(self, repo: Path) -> None:
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        (repo / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
        patch = workspace_diff(repo, written_paths=("helper.py",))
        assert "+++ b/helper.py" in patch
        assert "+VALUE = 2" in patch
        assert "+++ b/app.py" in patch
        # A path that is not on disk contributes nothing.
        assert "+++ b/missing.py" not in workspace_diff(
            repo, written_paths=("missing.py",)
        )

    def test_non_repo_returns_empty(self, tmp_path: Path) -> None:
        # ``tmp_path`` is nested inside this checkout; the outer repository must
        # never be reviewed on the run's behalf.
        assert workspace_diff(tmp_path) == ""


class TestGate:
    def test_blocks_on_test_file_edit(self, repo: Path) -> None:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_app.py").write_text("def test_x():\n    assert 1\n", "utf-8")
        gate = CompletionReviewGate()
        result = gate.review(repo, base_commit=_head(repo), written_paths=("tests/test_app.py",))
        assert result.blocked is True
        assert gate.blocks == 1
        codes = {finding.code for finding in result.findings}
        assert "test_file_modified" in codes
        payload = json.loads(result.to_tool_error())
        assert payload["error"] == "completion_review_blocked"
        assert "tests/test_app.py" in payload["hint"]

    def test_blocks_on_verification_bypass(self, repo: Path) -> None:
        (repo / "app.py").write_text(
            "import pytest\n\npytest.mark.skip(reason='todo')\n", encoding="utf-8"
        )
        result = CompletionReviewGate().review(repo, base_commit=_head(repo))
        assert result.blocked is True
        assert {finding.code for finding in result.findings} == {"verification_bypass"}

    def test_blocks_on_failed_test_run(self, repo: Path) -> None:
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        result = CompletionReviewGate().review(
            repo,
            base_commit=_head(repo),
            test_runs=[TestRun("pytest -q", 1)],
        )
        assert result.blocked is True
        assert "tests_failed" in {finding.code for finding in result.findings}

    def test_missing_test_evidence_is_advisory(self, repo: Path) -> None:
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        result = CompletionReviewGate().review(repo, base_commit=_head(repo))
        assert result.blocked is False
        assert result.findings == ()
        assert "no_test_evidence" in {finding.code for finding in result.advisory}

    def test_passing_tests_approve(self, repo: Path) -> None:
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        result = CompletionReviewGate().review(
            repo,
            base_commit=_head(repo),
            test_runs=[TestRun("pytest -q", 0)],
        )
        assert result.blocked is False
        assert result.findings == ()
        assert result.advisory == ()
        assert result.verdict is not None
        assert result.verdict.decision.value == "approve"

    def test_docs_only_skips_test_requirement(self, repo: Path) -> None:
        (repo / "README.md").write_text("# docs\n", encoding="utf-8")
        result = CompletionReviewGate().review(repo, base_commit=_head(repo))
        assert result.blocked is False
        assert result.findings == ()

    def test_empty_diff_is_skipped(self, repo: Path) -> None:
        result = CompletionReviewGate().review(repo, base_commit=_head(repo))
        assert result.skipped == "no_diff"
        assert result.verdict is None
        assert result.blocked is False

    def test_only_one_block_per_run(self, repo: Path) -> None:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_app.py").write_text("def test_x():\n    assert 1\n", "utf-8")
        gate = CompletionReviewGate(max_blocks=1)
        first = gate.review(repo, base_commit=_head(repo), written_paths=("tests/test_app.py",))
        second = gate.review(repo, base_commit=_head(repo), written_paths=("tests/test_app.py",))
        assert first.blocked is True
        assert second.blocked is False
        assert second.findings  # still reported, just not blocking twice
        assert gate.blocks == 1
        assert gate.to_dict()["blocks"] == 1

    def test_zero_max_blocks_never_blocks(self, repo: Path) -> None:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_app.py").write_text("def test_x():\n    assert 1\n", "utf-8")
        result = CompletionReviewGate(max_blocks=0).review(repo, base_commit=_head(repo))
        assert result.blocked is False

    def test_negative_max_blocks_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            CompletionReviewGate(max_blocks=-1)

    def test_advisory_codes_are_the_documented_ones(self) -> None:
        assert ADVISORY_CODES == {"empty_patch", "no_test_evidence", "tests_not_executed"}


class TestHarnessWiring:
    def test_done_is_blocked_once_for_a_tampered_test_file(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(COMPLETION_REVIEW_ENABLED_ENV, "1")
        (repo / "tests").mkdir()
        responses = iter(
            [
                _resp(
                    [
                        (
                            "write_file",
                            {
                                "path": "tests/test_app.py",
                                "content": "def test_x():\n    assert 1\n",
                            },
                        )
                    ]
                ),
                _resp([("done", {"summary": "tests added"})]),
                _resp([("done", {"summary": "tests added"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        blocks = [ln for ln in lines if ln.get("event") == "completion_review_block"]
        assert len(blocks) == 1
        assert blocks[0]["findings"][0]["code"] == "test_file_modified"
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is True
        assert final["completion_review"]["blocks"] == 1

    def test_advisory_gap_does_not_block_completion(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(COMPLETION_REVIEW_ENABLED_ENV, "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "app.py", "content": "print('v2')\n"})]),
                _resp([("done", {"summary": "updated"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        assert [ln for ln in lines if ln.get("event") == "completion_review_block"] == []
        reviews = [ln for ln in lines if ln.get("event") == "completion_review"]
        assert reviews[-1]["blocked"] is False
        assert "no_test_evidence" in {row["code"] for row in reviews[-1]["advisory"]}
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is True

    def test_passing_tests_clear_the_review(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(COMPLETION_REVIEW_ENABLED_ENV, "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "app.py", "content": "print('v2')\n"})]),
                _resp([("run_command", {"command": "pytest -q"})]),
                _resp([("done", {"summary": "done"})]),
            ]
        )
        runner = AsyncMock(return_value=json.dumps({"exit_code": 0, "output": "3 passed"}))
        with (
            patch.object(hll, "_completion", side_effect=lambda m: next(responses)),
            patch.object(hll, "_tool_run_command", runner),
        ):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        reviews = [ln for ln in lines if ln.get("event") == "completion_review"]
        assert reviews[-1]["decision"] == "approve"
        assert reviews[-1]["tests_run"] == 1
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["completion_review"]["last"]["decision"] == "approve"

    def test_docs_only_task_completes_without_tests(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(COMPLETION_REVIEW_ENABLED_ENV, "1")
        responses = iter(
            [
                _resp([("write_file", {"path": "README.md", "content": "# docs\n"})]),
                _resp([("done", {"summary": "documented"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        reviews = [ln for ln in lines if ln.get("event") == "completion_review"]
        assert reviews[-1]["blocked"] is False
        assert "advisory" not in reviews[-1]

    def test_disabled_flag_keeps_the_legacy_loop(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.delenv(COMPLETION_REVIEW_ENABLED_ENV, raising=False)
        (repo / "tests").mkdir()
        responses = iter(
            [
                _resp(
                    [
                        (
                            "write_file",
                            {
                                "path": "tests/test_app.py",
                                "content": "def test_x():\n    assert 1\n",
                            },
                        )
                    ]
                ),
                _resp([("done", {"summary": "tests added"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(repo), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is True
        assert "completion_review" not in final
        assert [ln for ln in lines if ln.get("event") == "completion_review_block"] == []

    def test_exit_code_parser_reads_the_payload(self) -> None:
        assert hll._command_exit_code(json.dumps({"exit_code": 3})) == 3
        assert hll._command_exit_code(json.dumps({"exit_code": 0})) == 0
        assert hll._command_exit_code(json.dumps({"output": "x"})) is None
        assert hll._command_exit_code("not json") is None
        assert hll._command_exit_code(json.dumps({"exit_code": True})) is None

    def test_module_flag_constant_matches_env(self) -> None:
        assert cr.COMPLETION_REVIEW_ENABLED_ENV == COMPLETION_REVIEW_ENABLED_ENV
