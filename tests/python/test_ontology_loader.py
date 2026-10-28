"""CONTEXT ontology loader contracts (T19 / D9).

Guards: UC_ONTOLOGY_MCP default off; soft empty when flag off or CONTEXT
absent; seeded parse + list/lookup; no write API; no live MCP server.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent import ontology as ontology_mod  # noqa: E402
from ultimate_coders.agent.ontology import (  # noqa: E402
    ONTOLOGY_MCP_ENV,
    OntologyTerm,
    list_terms,
    load_ontology,
    lookup_term,
    ontology_mcp_enabled,
    parse_context_markdown,
)

_SEED_NAMES = (
    "Gateway",
    "Worker",
    "Task",
    "Subtask",
    "Attempt",
    "ExecutionEnvelope",
    "CONTRACT_VERSION",
)

_FIXTURE = """# Fixture Context

## Language

**Gateway**:
Independently deployed Orchestrator-side control plane.
_Avoid_: Orchestrator server, control plane

**Worker**:
Independently deployed Python execution unit.
_Avoid_: Agent host

**OddTerm**:
"""


def test_ontology_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ONTOLOGY_MCP_ENV, raising=False)
    assert ontology_mcp_enabled() is False
    assert load_ontology() == []
    assert list_terms() == []
    assert lookup_term("Gateway") is None


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_ontology_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, value)
    assert ontology_mcp_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe", ""])
def test_ontology_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, value)
    assert ontology_mcp_enabled() is False


def test_load_soft_empty_when_flag_off_even_with_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ONTOLOGY_MCP_ENV, raising=False)
    context = tmp_path / "CONTEXT.md"
    context.write_text(_FIXTURE, encoding="utf-8")
    assert load_ontology(path=context) == []
    assert lookup_term("Gateway", path=context) is None


def test_load_soft_empty_when_context_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    missing = tmp_path / "no-such-CONTEXT.md"
    assert load_ontology(path=missing) == []
    assert list_terms(path=missing) == []
    assert lookup_term("Gateway", path=missing) is None


def test_load_soft_empty_when_context_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    # Directory path → read_text raises IsADirectoryError / OSError.
    assert load_ontology(path=tmp_path) == []


def test_parse_and_lookup_seeded_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    context = tmp_path / "CONTEXT.md"
    context.write_text(_FIXTURE, encoding="utf-8")

    terms = load_ontology(path=context)
    names = [t.name for t in terms]
    assert names == ["Gateway", "Worker"]
    assert terms[0].authority_path == "CONTEXT.md"
    assert terms[0].avoid == ("Orchestrator server", "control plane")
    assert "control plane" in terms[0].definition

    listed = list_terms(path=context)
    assert listed == terms

    hit = lookup_term("gateway", path=context)
    assert hit is not None
    assert hit.name == "Gateway"
    assert lookup_term("missing", path=context) is None
    # Empty / whitespace needle soft-misses.
    assert lookup_term("  ", path=context) is None


def test_parse_skips_definitionless_term() -> None:
    terms = parse_context_markdown(_FIXTURE)
    assert [t.name for t in terms] == ["Gateway", "Worker"]


def test_enabled_override_bypasses_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ONTOLOGY_MCP_ENV, raising=False)
    context = tmp_path / "CONTEXT.md"
    context.write_text(_FIXTURE, encoding="utf-8")
    terms = load_ontology(path=context, enabled=True)
    assert [t.name for t in terms] == ["Gateway", "Worker"]
    assert load_ontology(path=context, enabled=False) == []


def test_seeded_repo_context_parses_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    seeded = REPO / "CONTEXT.md"
    assert seeded.is_file(), "T19 must seed root CONTEXT.md"
    terms = load_ontology(path=seeded)
    names = {t.name for t in terms}
    assert set(_SEED_NAMES).issubset(names)
    for name in _SEED_NAMES:
        hit = lookup_term(name.lower(), path=seeded)
        assert hit is not None
        assert hit.name == name
        assert hit.definition
        assert hit.authority_path == "CONTEXT.md"


def test_no_ontology_write_api_surface() -> None:
    forbidden_prefixes = ("write_", "delete_", "remember_", "save_", "mutate_")
    public = [
        name
        for name, obj in inspect.getmembers(ontology_mod)
        if inspect.isfunction(obj) and not name.startswith("_")
    ]
    offenders = [
        name
        for name in public
        if any(name.startswith(prefix) for prefix in forbidden_prefixes)
    ]
    assert offenders == []
    # Read surface required for T20.
    assert {"ontology_mcp_enabled", "load_ontology", "list_terms", "lookup_term"} <= set(
        public
    )
    assert OntologyTerm.__dataclass_fields__  # type: ignore[attr-defined]


def test_module_does_not_import_hy_memory_or_codegraph() -> None:
    source = Path(ontology_mod.__file__).read_text(encoding="utf-8")
    assert "hy_memory" not in source
    assert "codegraph" not in source


def test_default_path_uses_cwd_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    context = tmp_path / "CONTEXT.md"
    context.write_text(_FIXTURE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    terms = load_ontology()
    assert [t.name for t in terms] == ["Gateway", "Worker"]


def test_parse_empty_and_blank_header() -> None:
    assert parse_context_markdown("") == []
    assert parse_context_markdown("   \n") == []
    assert parse_context_markdown("****:\norphan definition\n") == []
