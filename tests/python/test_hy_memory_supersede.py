"""Supersede-not-delete contracts for CodingMemory.remember (T11 / D6).

Guards: remember stamps supersede metadata, returns superseded_ids, and never
invokes delete/remove/drop (or tombstone) on the Hy-Memory client. MCP
remember_coding_memory forwards supersede_fact_ids through CodingMemory.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent import engine_mcp, hy_memory  # noqa: E402
from ultimate_coders.agent.hy_memory import (  # noqa: E402
    SUPERSEDE_POLICY_MARK_NOT_DELETE,
    SUPERSEDE_POLICY_METADATA_KEY,
    SUPERSEDES_METADATA_KEY,
    CodingMemory,
)

FORBIDDEN_MUTATION_NAMES = (
    "delete",
    "remove",
    "drop",
    "tombstone",
    "delete_fact",
    "remove_fact",
    "drop_memory",
)


class RecordingClient:
    """Fake Hy-Memory client that records every attribute access / call.

    Exposes add plus the forbidden mutation names so tests can prove remember
    never touches them. Each forbidden method increments a counter when called.
    """

    def __init__(self, *, add_result: dict[str, Any] | None = None) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.mutation_calls: list[str] = []
        self._add_result = add_result or {
            "success": True,
            "scene": "productivity",
            "fact_ids": ["f-new"],
            "experience_ids": ["e-1"],
            "chain_rounds": 1,
        }
        for name in FORBIDDEN_MUTATION_NAMES:
            setattr(self, name, self._make_forbidden(name))

    def _make_forbidden(self, name: str):
        def _forbidden(*_args: Any, **_kwargs: Any) -> None:
            self.mutation_calls.append(name)
            raise AssertionError(f"remember must not call client.{name}")

        return _forbidden

    def add(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append({"data": data, **kwargs})
        return dict(self._add_result)

    def close(self) -> None:
        return None


def _trace() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Replace the timeout implementation."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": '{"path": "src/search.py"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "apply_patch",
            "content": "Patch applied",
        },
    ]


def test_remember_stamps_supersede_metadata_and_return() -> None:
    client = RecordingClient()
    mem = CodingMemory(user_id="u", workspace_id="w", branch="b", client=client)

    result = mem.remember(
        _trace(),
        metadata={"task_id": "t-1", "note": "keep-me"},
        supersede_fact_ids=["f-old", "f-older", "f-old"],
    )

    assert result["success"] is True
    assert result["fact_ids"] == ["f-new"]
    assert result["superseded_ids"] == ["f-old", "f-older"]
    assert len(client.add_calls) == 1
    meta = client.add_calls[0]["metadata"]
    assert meta["task_id"] == "t-1"
    assert meta["note"] == "keep-me"
    assert meta[SUPERSEDES_METADATA_KEY] == ["f-old", "f-older"]
    assert meta[SUPERSEDE_POLICY_METADATA_KEY] == SUPERSEDE_POLICY_MARK_NOT_DELETE
    assert client.mutation_calls == []


def test_remember_never_calls_delete_remove_or_drop() -> None:
    client = RecordingClient()
    mem = CodingMemory(user_id="u", client=client)

    mem.remember(_trace(), supersede_fact_ids=["f-prior"])
    mem.remember(_trace())  # no supersede either

    assert client.mutation_calls == []
    assert len(client.add_calls) == 2
    for name in FORBIDDEN_MUTATION_NAMES:
        assert callable(getattr(client, name))  # methods exist but unused


def test_remember_without_supersede_leaves_metadata_unchanged() -> None:
    client = RecordingClient()
    mem = CodingMemory(user_id="u", client=client)
    original = {"task_id": "t-1"}

    result = mem.remember(_trace(), metadata=original)

    assert "superseded_ids" not in result
    assert client.add_calls[0]["metadata"] is original
    assert original == {"task_id": "t-1"}
    assert client.mutation_calls == []


def test_remember_detects_supersedes_already_in_metadata() -> None:
    client = RecordingClient()
    mem = CodingMemory(user_id="u", client=client)

    result = mem.remember(
        _trace(),
        metadata={"supersedes": ["f-meta"], "task_id": "t-2"},
    )

    assert result["superseded_ids"] == ["f-meta"]
    meta = client.add_calls[0]["metadata"]
    assert meta[SUPERSEDES_METADATA_KEY] == ["f-meta"]
    assert meta[SUPERSEDE_POLICY_METADATA_KEY] == SUPERSEDE_POLICY_MARK_NOT_DELETE
    assert meta["task_id"] == "t-2"
    assert client.mutation_calls == []


def test_remember_unions_arg_ids_with_metadata_supersedes() -> None:
    client = RecordingClient()
    mem = CodingMemory(user_id="u", client=client)

    result = mem.remember(
        _trace(),
        metadata={"supersedes": ["f-meta", "f-shared"]},
        supersede_fact_ids=["f-shared", "f-arg"],
    )

    assert result["superseded_ids"] == ["f-meta", "f-shared", "f-arg"]
    assert client.add_calls[0]["metadata"][SUPERSEDES_METADATA_KEY] == [
        "f-meta",
        "f-shared",
        "f-arg",
    ]
    assert client.mutation_calls == []


def test_remember_ignores_blank_supersede_ids() -> None:
    client = RecordingClient()
    mem = CodingMemory(user_id="u", client=client)

    result = mem.remember(_trace(), supersede_fact_ids=["", "  ", None])  # type: ignore[list-item]

    assert "superseded_ids" not in result
    assert client.add_calls[0]["metadata"] is None
    assert client.mutation_calls == []


def test_remember_treats_bare_string_supersede_id_as_one_id() -> None:
    """str is a Sequence at runtime; must not iterate into characters."""
    client = RecordingClient()
    mem = CodingMemory(user_id="u", client=client)

    result = mem.remember(_trace(), supersede_fact_ids="fact-one")  # type: ignore[arg-type]

    assert result["superseded_ids"] == ["fact-one"]
    assert client.add_calls[0]["metadata"][SUPERSEDES_METADATA_KEY] == ["fact-one"]
    assert client.mutation_calls == []


def _require_mcp() -> None:
    if engine_mcp.Server is None:
        pytest.skip("mcp package not installed")


def test_mcp_remember_forwards_supersede_fact_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_mcp()
    client = RecordingClient()
    backend = CodingMemory(user_id="u", workspace_id="w", branch="b", client=client)
    monkeypatch.setattr(engine_mcp, "_MEMORY_BACKEND", backend)

    out = asyncio.run(
        engine_mcp._remember_coding_memory(
            None,
            {
                "messages": _trace(),
                "supersede_fact_ids": ["f-mcp-old"],
            },
        )
    )

    assert "1 fact(s), 1 experience(s)" in out[0].text
    assert "superseded 1 prior fact(s)" in out[0].text
    meta = client.add_calls[0]["metadata"]
    assert meta[SUPERSEDES_METADATA_KEY] == ["f-mcp-old"]
    assert meta[SUPERSEDE_POLICY_METADATA_KEY] == SUPERSEDE_POLICY_MARK_NOT_DELETE
    assert client.mutation_calls == []


def test_mcp_remember_tool_schema_lists_supersede_fact_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_mcp()
    monkeypatch.setattr(engine_mcp, "hy_memory_available", lambda: True)
    tools = {t.name: t for t in engine_mcp._memory_tools()}
    tool = tools["remember_coding_memory"]
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(by_alias=True)
    elif hasattr(schema, "dict"):
        schema = schema.dict()
    assert "supersede_fact_ids" in schema["properties"]
    assert schema["properties"]["supersede_fact_ids"]["type"] == "array"


def test_version_pin_and_allowlist_unchanged() -> None:
    """T11 must not loosen HY_MEMORY_MIN_VERSION or the MEMORY_* allowlist."""
    assert hy_memory.HY_MEMORY_MIN_VERSION == "2.0.0"
    assert "MEMORY_LLM_API_KEY" in hy_memory.HY_MEMORY_ENV_ALLOWLIST
    assert len(hy_memory.HY_MEMORY_ENV_ALLOWLIST) == 10
