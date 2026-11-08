"""Tests for scripts/check-replan-execute-readiness.py (V8 T18d / D14).

Pins:
  * truthy gate matches UC enforce flags (1/true/yes/on, trim+case-insensitive)
  * dry-run never mutates env or Compose
  * hard-fail when the readiness runbook is missing
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-replan-execute-readiness.py"
RUNBOOK = REPO / "docs" / "architecture" / "replan-execute-readiness-runbook.md"
COMPOSE_CANDIDATES = (
    REPO / "docker" / "docker-compose.yml",
    REPO / "docker" / "docker-compose.gateway.yml",
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_replan_execute_readiness", GUARD
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    return _load()


def test_enforce_flag_truthy_matches_uc_tokens(guard) -> None:
    for value in ("1", "true", "TRUE", "Yes", "on", "  ON  "):
        assert guard.enforce_flag_enabled(value) is True, value
    for value in (None, "", "  ", "0", "false", "off", "enabled", "OnX"):
        assert guard.enforce_flag_enabled(value) is False, value


def test_hard_fail_when_runbook_missing(guard, tmp_path: pathlib.Path) -> None:
    missing_rel = "docs/architecture/replan-execute-readiness-runbook.md"
    blockers = guard.hard_blockers(
        {},
        runbook_rel=missing_rel,
        repo=tmp_path,
    )
    assert len(blockers) == 1
    assert missing_rel in blockers[0]
    assert "MISSING" in blockers[0] or "missing" in blockers[0].lower()

    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    (tmp_path / missing_rel).write_text("# runbook\n", encoding="utf-8")
    assert (
        guard.hard_blockers({}, runbook_rel=missing_rel, repo=tmp_path) == []
    )


def test_checklist_reports_precursor_flags_and_runbook(
    guard, tmp_path: pathlib.Path
) -> None:
    runbook_rel = "docs/architecture/replan-execute-readiness-runbook.md"
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    (tmp_path / runbook_rel).write_text("# runbook\n", encoding="utf-8")

    env = {
        guard.PROPOSAL_ENV: "1",
        guard.CHECKLIST_ENV: "true",
        guard.ACCOUNTING_ENV: "yes",
        guard.COMPLETION_ENV: "on",
    }
    lines = guard.assemble_checklist(
        env, runbook_rel=runbook_rel, repo=tmp_path
    )
    joined = "\n".join(lines)
    assert guard.PROPOSAL_ENV in joined
    assert guard.CHECKLIST_ENV in joined
    assert guard.ACCOUNTING_ENV in joined
    assert guard.COMPLETION_ENV in joined
    assert "ON" in joined
    assert runbook_rel in joined
    assert "(present)" in joined
    assert "deferred" in joined.lower() or "does not enable execute" in joined.lower()

    lines_off = guard.assemble_checklist(
        {
            guard.PROPOSAL_ENV: None,
            guard.CHECKLIST_ENV: "0",
            guard.ACCOUNTING_ENV: "false",
            guard.COMPLETION_ENV: "",
        },
        runbook_rel=runbook_rel,
        repo=tmp_path,
    )
    joined_off = "\n".join(lines_off)
    assert "off" in joined_off
    assert "(present)" in joined_off


def test_main_exit_codes(guard, tmp_path: pathlib.Path, capsys, monkeypatch) -> None:
    # Point REPO-relative lookup at a temp tree missing the runbook → fail.
    monkeypatch.setattr(guard, "REPO", tmp_path)
    assert guard.main(env={}) == 1
    out_fail = capsys.readouterr().out
    assert "HARD BLOCKERS" in out_fail
    assert "failed" in out_fail

    runbook_rel = guard.RUNBOOK_REL
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    (tmp_path / runbook_rel).write_text("# runbook\n", encoding="utf-8")
    assert guard.main(env={}) == 0
    out_ok = capsys.readouterr().out
    assert "passed" in out_ok


def test_dry_run_never_mutates_env_or_compose(
    guard, tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The readiness probe must be side-effect free on env and Compose files."""
    watched = (
        guard.PROPOSAL_ENV,
        guard.CHECKLIST_ENV,
        guard.ACCOUNTING_ENV,
        guard.COMPLETION_ENV,
    )
    before = {name: os.environ.get(name) for name in watched}
    compose_mtimes = {
        p: p.stat().st_mtime_ns for p in COMPOSE_CANDIDATES if p.is_file()
    }
    assert compose_mtimes, "expected at least one Compose file to pin non-mutation"

    # Pass path (real repo runbook present).
    assert guard.main(env={name: None for name in watched}) == 0
    # Fail path (missing runbook) without touching process env assignment.
    monkeypatch.setattr(guard, "REPO", tmp_path)
    assert guard.main(env={name: None for name in watched}) == 1

    for name, value in before.items():
        assert os.environ.get(name) == value
    for path, mtime in compose_mtimes.items():
        assert path.stat().st_mtime_ns == mtime, path

    src = GUARD.read_text(encoding="utf-8")
    assert "os.environ[" not in src
    assert "os.putenv" not in src
    assert "os.environ.setdefault" not in src
    assert "write_text" not in src
    assert "Path.write" not in src
    assert "open(" not in src


def test_runbook_exists_for_pointer() -> None:
    assert RUNBOOK.is_file(), f"missing runbook at {RUNBOOK}"
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "UC_REPLAN_PROPOSAL_ENFORCE" in text
    assert "UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE" in text
    assert "UC_REPLAN_ACCOUNTING_ENFORCE" in text
    assert "UC_COMPLETION_CONDITIONS_ENFORCE" in text
    assert "admit_execution" in text
    assert "18-true-replan-deferred" in text or "ticket 18" in text.lower()
    assert "durable-runtime-upgrade-notes" in text
    assert "check-replan-execute-readiness.py" in text
    lower = text.lower()
    assert "deferred" in lower
    assert "does not" in lower or "never" in lower
