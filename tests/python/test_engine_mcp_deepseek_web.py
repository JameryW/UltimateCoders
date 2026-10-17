"""Worker MCP registration for DeepSeek Search/Fetch tools."""

from __future__ import annotations

from ultimate_coders.agent import engine_mcp


def test_deepseek_tools_absent_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # When mcp package is missing, helpers return []; when present, still
    # gated on the API key.
    names = [t.name for t in engine_mcp._deepseek_web_tools()]
    assert names == []


def test_deepseek_tools_present_with_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    if engine_mcp.Server is None:
        # Optional mcp dependency not installed in this environment.
        assert engine_mcp._deepseek_web_tools() == []
        return
    names = [t.name for t in engine_mcp._deepseek_web_tools()]
    assert names == ["deepseek_web_search", "deepseek_web_fetch"]
