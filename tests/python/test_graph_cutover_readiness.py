"""Tests for scripts/check-graph-cutover-readiness.py (V8 P4 / D10).

Pins:
  * gate parsing aligns with gateway ``resolve_graph_shadow_choice`` (exact ``on``)
  * dry-run never mutates env or Compose
  * hard-fail when shadow is on without ``UC_DATABASE_URL``
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-graph-cutover-readiness.py"
RUNBOOK = REPO / "docs" / "architecture" / "graph-cutover-runbook.md"
COMPOSE_CANDIDATES = (
    REPO / "docker" / "docker-compose.yml",
    REPO / "docker" / "docker-compose.gateway.yml",
)


def _load():
    spec = importlib.util.spec_from_file_location("check_graph_cutover_readiness", GUARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    return _load()


def test_gate_parsing_matches_gateway_exact_on(guard) -> None:
    assert guard.resolve_graph_shadow_choice("on") is True
    for value in (None, "", "OFF", "On", "1", "true", "yes", "ON"):
        assert guard.resolve_graph_shadow_choice(value) is False, value


def test_database_url_is_set_rejects_blank(guard) -> None:
    assert guard.database_url_is_set("postgres://localhost/uc") is True
    assert guard.database_url_is_set(None) is False
    assert guard.database_url_is_set("") is False
    assert guard.database_url_is_set("   ") is False


def test_hard_fail_when_shadow_on_without_database_url(guard) -> None:
    blockers = guard.hard_blockers(
        {guard.SHADOW_ENV: "on", guard.DATABASE_ENV: None}
    )
    assert len(blockers) == 1
    assert guard.SHADOW_ENV in blockers[0]
    assert guard.DATABASE_ENV in blockers[0]

    assert guard.hard_blockers(
        {guard.SHADOW_ENV: "on", guard.DATABASE_ENV: "postgres://x"}
    ) == []
    # Non-exact tokens are off — not a hard blocker even without DB URL.
    assert guard.hard_blockers(
        {guard.SHADOW_ENV: "true", guard.DATABASE_ENV: None}
    ) == []
    assert guard.hard_blockers(
        {guard.SHADOW_ENV: None, guard.DATABASE_ENV: None}
    ) == []


def test_checklist_reports_shadow_db_and_runbook(guard, tmp_path: pathlib.Path) -> None:
    runbook_rel = "docs/architecture/graph-cutover-runbook.md"
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    (tmp_path / runbook_rel).write_text("# runbook\n", encoding="utf-8")

    lines = guard.assemble_checklist(
        {guard.SHADOW_ENV: "on", guard.DATABASE_ENV: "postgres://x"},
        runbook_rel=runbook_rel,
        repo=tmp_path,
    )
    joined = "\n".join(lines)
    assert "ON (exact on)" in joined
    assert f"{guard.DATABASE_ENV}: set" in joined
    assert runbook_rel in joined
    assert "(present)" in joined

    lines_off = guard.assemble_checklist(
        {guard.SHADOW_ENV: "true", guard.DATABASE_ENV: None},
        runbook_rel=runbook_rel,
        repo=tmp_path,
    )
    joined_off = "\n".join(lines_off)
    assert "off" in joined_off
    assert f"{guard.DATABASE_ENV}: unset/empty" in joined_off


def test_main_exit_codes(guard, capsys) -> None:
    assert (
        guard.main(
            env={guard.SHADOW_ENV: "on", guard.DATABASE_ENV: "postgres://x"}
        )
        == 0
    )
    out_ok = capsys.readouterr().out
    assert "passed" in out_ok
    assert RUNBOOK.as_posix().split("/")[-1] in out_ok or guard.RUNBOOK_REL in out_ok

    assert guard.main(env={guard.SHADOW_ENV: "on", guard.DATABASE_ENV: ""}) == 1
    out_fail = capsys.readouterr().out
    assert "HARD BLOCKERS" in out_fail
    assert "failed" in out_fail


def test_dry_run_never_mutates_env_or_compose(guard) -> None:
    """The readiness probe must be side-effect free on env and Compose files."""
    before_shadow = os.environ.get(guard.SHADOW_ENV)
    before_db = os.environ.get(guard.DATABASE_ENV)
    compose_mtimes = {
        p: p.stat().st_mtime_ns for p in COMPOSE_CANDIDATES if p.is_file()
    }
    assert compose_mtimes, "expected at least one Compose file to pin non-mutation"

    # Exercise both pass and fail paths without touching process env assignment.
    assert (
        guard.main(
            env={guard.SHADOW_ENV: None, guard.DATABASE_ENV: "postgres://x"}
        )
        == 0
    )
    assert (
        guard.main(env={guard.SHADOW_ENV: "on", guard.DATABASE_ENV: None}) == 1
    )

    assert os.environ.get(guard.SHADOW_ENV) == before_shadow
    assert os.environ.get(guard.DATABASE_ENV) == before_db
    for path, before in compose_mtimes.items():
        assert path.stat().st_mtime_ns == before, path

    # Guard source itself must not assign into os.environ or write Compose.
    src = GUARD.read_text(encoding="utf-8")
    assert "os.environ[" not in src
    assert "os.putenv" not in src
    assert "os.environ.setdefault" not in src
    assert "write_text" not in src
    assert "Path.write" not in src
    assert "open(" not in src  # no file writes; Path.is_file is enough for the runbook probe


def test_runbook_exists_for_pointer() -> None:
    assert RUNBOOK.is_file(), f"missing runbook at {RUNBOOK}"
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "UC_GRAPH_SHADOW=on" in text
    assert "resolve_graph_shadow_choice" in text
    assert "durable-runtime-upgrade-notes.md" in text
    assert "defaults stay off" in text.lower() or "remain **off**" in text or "stay **off**" in text
