"""Ontology MCP read-only tools (T20 / D9).

Guards: UC_ONTOLOGY_MCP default off → tools absent; flag on → list/lookup
advertised and wrap ontology.list_terms / lookup_term; soft empty / not-found
when CONTEXT missing; no write/remember/delete ontology tools. Helper-based —
no full coding agent / stdio MCP server.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent import engine_mcp  # noqa: E402
from ultimate_coders.agent.ontology import (  # noqa: E402
    ONTOLOGY_MCP_ENV,
    OntologyTerm,
)

_SEED_FIXTURE = """# Fixture Context

## Language

**Gateway**:
Independently deployed Orchestrator-side control plane.
_Avoid_: Orchestrator server, control plane

**Worker**:
Independently deployed Python execution unit.
"""

_WRITE_TOOL_FRAGMENTS = (
    "write_ontology",
    "remember_ontology",
    "delete_ontology",
    "mutate_ontology",
    "save_ontology",
)


def _require_mcp() -> None:
    if engine_mcp.Server is None:
        pytest.skip("mcp package not installed")


def test_ontology_tools_absent_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ONTOLOGY_MCP_ENV, raising=False)
    assert engine_mcp._ontology_tools() == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_ontology_tools_absent_when_flag_falsey(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, value)
    assert engine_mcp._ontology_tools() == []


def test_ontology_tools_present_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    if engine_mcp.Server is None:
        assert engine_mcp._ontology_tools() == []
        return
    names = [t.name for t in engine_mcp._ontology_tools()]
    assert names == [
        engine_mcp.TOOL_LIST_ONTOLOGY,
        engine_mcp.TOOL_LOOKUP_ONTOLOGY,
    ]
    assert names == ["list_ontology_terms", "lookup_ontology_term"]


def test_ontology_tools_hidden_without_mcp_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    monkeypatch.setattr(engine_mcp, "Server", None)
    assert engine_mcp._ontology_tools() == []


def test_ontology_tools_no_write_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    if engine_mcp.Server is None:
        pytest.skip("mcp package not installed")
    names = [t.name for t in engine_mcp._ontology_tools()]
    lowered = " ".join(names).lower()
    for fragment in _WRITE_TOOL_FRAGMENTS:
        assert fragment not in lowered
    assert not any(
        any(tok in n.lower() for tok in ("write_", "remember_", "delete_", "mutate_"))
        for n in names
    )


def test_list_ontology_handler_seeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_mcp()
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    context = tmp_path / "CONTEXT.md"
    context.write_text(_SEED_FIXTURE, encoding="utf-8")
    out = asyncio.run(engine_mcp._list_ontology_terms({"path": str(context)}))
    text = out[0].text
    assert "**Gateway**:" in text
    assert "**Worker**:" in text
    assert "authority_path:" in text
    assert "Independently deployed Orchestrator-side control plane." in text


def test_lookup_ontology_handler_seeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_mcp()
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    context = tmp_path / "CONTEXT.md"
    context.write_text(_SEED_FIXTURE, encoding="utf-8")
    out = asyncio.run(
        engine_mcp._lookup_ontology_term({"name": "gateway", "path": str(context)})
    )
    text = out[0].text
    assert "**Gateway**:" in text
    assert "authority_path:" in text
    assert "not found" not in text.lower()


def test_list_ontology_handler_soft_empty_missing_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_mcp()
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    missing = tmp_path / "no-CONTEXT.md"
    out = asyncio.run(engine_mcp._list_ontology_terms({"path": str(missing)}))
    assert "No ontology terms found." in out[0].text


def test_lookup_ontology_handler_soft_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_mcp()
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    missing = tmp_path / "no-CONTEXT.md"
    out = asyncio.run(
        engine_mcp._lookup_ontology_term(
            {"name": "Gateway", "path": str(missing)}
        )
    )
    assert "not found" in out[0].text.lower()


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ONTOLOGY_MCP_ENV, raising=False)
    from ultimate_coders.agent.ontology import ontology_mcp_enabled

    assert ontology_mcp_enabled() is False
    assert engine_mcp._ontology_tools() == []


def test_handlers_are_thin_wrappers_over_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers must call ontology APIs — not invent a second authority."""
    _require_mcp()
    monkeypatch.setenv(ONTOLOGY_MCP_ENV, "1")
    term = OntologyTerm(
        name="Gateway",
        definition="control plane",
        avoid=("Orchestrator server",),
        authority_path="CONTEXT.md",
    )
    calls: list[tuple] = []

    def fake_list(*, path=None, enabled=None):
        calls.append(("list", path))
        return [term]

    def fake_lookup(name, *, path=None, enabled=None, terms=None):
        calls.append(("lookup", name, path))
        return term if name.lower() == "gateway" else None

    monkeypatch.setattr(engine_mcp, "ontology_list_terms", fake_list)
    monkeypatch.setattr(engine_mcp, "ontology_lookup_term", fake_lookup)

    listed = asyncio.run(engine_mcp._list_ontology_terms({}))
    looked = asyncio.run(engine_mcp._lookup_ontology_term({"name": "gateway"}))
    assert calls[0][0] == "list"
    assert calls[1][0] == "lookup"
    assert "**Gateway**:" in listed[0].text
    assert "**Gateway**:" in looked[0].text
