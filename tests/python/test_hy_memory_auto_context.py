"""Optional Hy-Memory auto-context contracts (T12 / D6).

Guards: UC_HY_MEMORY_AUTO_CONTEXT default off; enabled + fake recall formats a
block; enabled + unavailable soft-skips; from_hy_memory_block uses Tier.MEMORY.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent.context_compiler import (  # noqa: E402
    Tier,
    compile_context,
    from_hy_memory_block,
    from_search_block,
)
from ultimate_coders.agent.hy_memory import (  # noqa: E402
    HY_MEMORY_AUTO_CONTEXT_ENV,
    CodingMemory,
    build_hy_memory_context_block,
    hy_memory_auto_context_enabled,
)


class FakeRecallClient:
    """Minimal client that only implements search (for recall)."""

    def __init__(self, payload: dict[str, Any] | None = None, *, raise_on_search: bool = False):
        self.payload = payload or {
            "facts": [{"statement": "kept the 5s timeout", "score": 0.9}],
            "experiences": [{"task": "fix search timeout", "solution": "5s cap"}],
            "chain_block": {"text": "v1 -> v2"},
        }
        self.raise_on_search = raise_on_search
        self.search_calls: list[dict[str, Any]] = []

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append({"query": query, **kwargs})
        if self.raise_on_search:
            raise RuntimeError("endpoint down")
        return dict(self.payload)

    def close(self) -> None:
        return None


def _mem(client: FakeRecallClient | None = None) -> CodingMemory:
    return CodingMemory(user_id="test", client=client or FakeRecallClient())


def test_auto_context_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HY_MEMORY_AUTO_CONTEXT_ENV, raising=False)
    assert hy_memory_auto_context_enabled() is False
    assert build_hy_memory_context_block("timeout", memory=_mem()) is None


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_auto_context_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(HY_MEMORY_AUTO_CONTEXT_ENV, value)
    assert hy_memory_auto_context_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe"])
def test_auto_context_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(HY_MEMORY_AUTO_CONTEXT_ENV, value)
    assert hy_memory_auto_context_enabled() is False


def test_build_block_injects_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HY_MEMORY_AUTO_CONTEXT_ENV, "1")
    client = FakeRecallClient()
    block = build_hy_memory_context_block("which timeout?", memory=_mem(client), limit=3)
    assert block is not None
    assert block.startswith("## Coding memory\n")
    assert "kept the 5s timeout" in block
    assert "5s cap" in block
    assert "v1 -> v2" in block
    assert client.search_calls[0]["query"] == "which timeout?"
    assert client.search_calls[0]["limit"] == 3


def test_build_block_soft_skips_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HY_MEMORY_AUTO_CONTEXT_ENV, "1")
    monkeypatch.setattr(
        "ultimate_coders.agent.hy_memory.hy_memory_available",
        lambda: False,
    )
    assert build_hy_memory_context_block("timeout") is None


def test_build_block_soft_skips_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HY_MEMORY_AUTO_CONTEXT_ENV, "1")
    assert build_hy_memory_context_block("  ", memory=_mem()) is None


def test_build_block_soft_skips_failed_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HY_MEMORY_AUTO_CONTEXT_ENV, "1")
    client = FakeRecallClient(raise_on_search=True)
    assert build_hy_memory_context_block("timeout", memory=_mem(client)) is None


def test_build_block_soft_skips_empty_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HY_MEMORY_AUTO_CONTEXT_ENV, "1")
    client = FakeRecallClient(payload={"facts": [], "experiences": [], "chain_block": None})
    assert build_hy_memory_context_block("timeout", memory=_mem(client)) is None


def test_from_hy_memory_block_tier_memory() -> None:
    items = from_hy_memory_block("## Coding memory\n- [fact] hello")
    assert len(items) == 1
    assert items[0].tier == Tier.MEMORY
    assert items[0].source == "hy-memory"
    assert items[0].item_id == "hy-memory-block"
    assert from_hy_memory_block(None) == []
    assert from_hy_memory_block("") == []


def test_render_legacy_appends_hy_memory_after_search() -> None:
    manifest = compile_context(
        from_search_block("## Search results\n- path/a.py:1 timeout helper")
        + from_hy_memory_block("## Coding memory\n- [fact] kept timeout"),
        budget_tokens=10_000,
    )
    text = manifest.render_legacy()
    assert "## Coding memory" in text
    assert "kept timeout" in text
    assert text.index("## Search results") < text.index("## Coding memory")
    assert manifest.summary()["items_by_tier"].get("P2 evidence") == 1
    assert manifest.summary()["items_by_tier"].get("P4 memory") == 1
