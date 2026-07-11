"""Unit tests for worker capability derivation and the uc-fs MCP server."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# uc-fs MCP tools are plain async functions when imported directly (no stdio serve).
from ultimate_coders.agent.fs_mcp import (
    _edit_file,
    _read_file,
    _resolve_workspace,
    _safe_path,
    _write_file,
)
from ultimate_coders.agent.sandbox import SandboxConfig
from ultimate_coders.agent.worker import Worker

# ── uc-fs path safety ──────────────────────────────────────────


class TestFsPathSafety:
    def test_safe_path_inside_workspace(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        assert _safe_path(ws, "src/main.py") == os.path.realpath(os.path.join(ws, "src/main.py"))

    def test_safe_path_rejects_escape(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            _safe_path(ws, "../../../etc/passwd")

    def test_safe_path_rejects_absolute_outside(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            _safe_path(ws, "/etc/passwd")

    def test_safe_path_rejects_empty(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="required"):
            _safe_path(str(tmp_path), "")

    def test_resolve_workspace_prefers_flag(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("UC_WORKSPACE", "/from/env")
        assert _resolve_workspace(str(tmp_path)) == os.path.realpath(str(tmp_path))


# ── uc-fs tool round-trip ──────────────────────────────────────


class TestFsTools:
    def test_write_read_roundtrip(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        asyncio.run(_write_file(ws, {"path": "a/b.txt", "content": "hello\nworld\n"}))
        out = asyncio.run(_read_file(ws, {"path": "a/b.txt"}))
        assert "1\thello" in out[0].text
        assert "2\tworld" in out[0].text

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        asyncio.run(_write_file(ws, {"path": "deep/nested/file.txt", "content": "x"}))
        assert (tmp_path / "deep" / "nested" / "file.txt").is_file()

    def test_edit_file_unique_replace(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        asyncio.run(_write_file(ws, {"path": "f.py", "content": "a = 1\nb = 2\n"}))
        out = asyncio.run(
            _edit_file(ws, {"path": "f.py", "old_string": "b = 2", "new_string": "b = 3"})
        )
        assert "Replaced 1" in out[0].text
        content = (tmp_path / "f.py").read_text()
        assert "b = 3" in content and "b = 2" not in content

    def test_edit_file_rejects_nonunique_without_replace_all(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        asyncio.run(_write_file(ws, {"path": "f.py", "content": "x\nx\n"}))
        out = asyncio.run(_edit_file(ws, {"path": "f.py", "old_string": "x", "new_string": "y"}))
        assert "matches 2" in out[0].text
        # File unchanged
        assert (tmp_path / "f.py").read_text() == "x\nx\n"

    def test_edit_file_replace_all(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        asyncio.run(_write_file(ws, {"path": "f.py", "content": "x\nx\n"}))
        out = asyncio.run(
            _edit_file(
                ws,
                {"path": "f.py", "old_string": "x", "new_string": "y", "replace_all": True},
            )
        )
        assert "Replaced 2" in out[0].text
        assert (tmp_path / "f.py").read_text() == "y\ny\n"

    def test_edit_file_not_found_message(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        out = asyncio.run(
            _edit_file(ws, {"path": "nope.py", "old_string": "a", "new_string": "b"})
        )
        assert "not found" in out[0].text

    def test_read_file_missing(self, tmp_path: Path) -> None:
        ws = str(tmp_path)
        out = asyncio.run(_read_file(ws, {"path": "missing.txt"}))
        assert "not found" in out[0].text

    def test_path_escape_returns_error_not_crash(self, tmp_path: Path) -> None:
        """_safe_path is the boundary that raises; server layer catches it."""
        ws = str(tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            _safe_path(ws, "../../escape.txt")


# ── _derive_capabilities ───────────────────────────────────────


class TestDeriveCapabilities:
    def _worker(self, stub_engine, **kwargs) -> Worker:
        # Pass a stub engine so auto-registration of uc-engine + uc-fs triggers.
        return Worker(engine=stub_engine, sandbox_config=SandboxConfig(**kwargs))

    def test_default_caps_include_core(self, stub_engine) -> None:
        caps = self._worker(stub_engine).capabilities
        for c in ("code", "search", "memory", "test", "decompose", "review"):
            assert c in caps, f"{c} missing"

    def test_uc_fs_auto_registered_derives_file_edit(self, stub_engine) -> None:
        caps = self._worker(stub_engine).capabilities
        assert "mcp:uc-fs" in caps
        assert "file-edit" in caps

    def test_codegraph_tools_derive_lsp(self, stub_engine) -> None:
        caps = self._worker(stub_engine, tools=["default", "mcp__codegraph__*"]).capabilities
        assert "codegraph" in caps
        assert "lsp" in caps

    def test_browser_debug_opt_in_via_env(self, stub_engine, monkeypatch) -> None:
        monkeypatch.setenv("UC_CAP_BROWSER", "1")
        monkeypatch.setenv("UC_CAP_DEBUG", "1")
        caps = self._worker(stub_engine).capabilities
        assert "browser" in caps
        assert "debug" in caps

    def test_browser_debug_absent_without_env(self, stub_engine) -> None:
        caps = self._worker(stub_engine).capabilities
        assert "browser" not in caps
        assert "debug" not in caps

    def test_custom_mcp_configs_keep_uc_fs(self, stub_engine) -> None:
        # Caller supplies own mcp_configs — uc-fs should still be appended
        cfg = SandboxConfig(mcp_configs=[{"external-srv": {"command": "x"}}])
        w = Worker(engine=stub_engine, sandbox_config=cfg)
        names = []
        for entry in w._sandbox_config.mcp_configs:
            if isinstance(entry, dict):
                names.extend(entry.keys())
        assert "external-srv" in names
        assert "uc-fs" in names
        assert "file-edit" in w.capabilities

    def test_no_engine_no_auto_mcp(self) -> None:
        # Legacy contract: engine=None + default SandboxConfig ⇒ no mcp capability
        w = Worker(engine=None, sandbox_config=SandboxConfig())
        assert "mcp" not in w.capabilities
        assert "file-edit" not in w.capabilities

    # ── agent CLI probing → capability advertisement ──────────────

    def test_codex_cli_present_advertises_codex(self, stub_engine, monkeypatch) -> None:
        """shutil.which("codex") truthy → "codex" capability advertised."""
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/codex" if cmd == "codex" else None)
        caps = self._worker(stub_engine).capabilities
        assert "codex" in caps
        assert "claude-code" not in caps

    def test_claude_cli_present_advertises_claude_code(self, stub_engine, monkeypatch) -> None:
        """shutil.which("claude") truthy → "claude-code" capability advertised."""
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/claude" if cmd == "claude" else None)
        caps = self._worker(stub_engine).capabilities
        assert "claude-code" in caps
        assert "codex" not in caps

    def test_both_clis_present_advertises_both(self, stub_engine, monkeypatch) -> None:
        """Both CLIs on PATH → both capabilities advertised."""
        monkeypatch.setattr(
            "shutil.which",
            lambda cmd: f"/usr/local/bin/{cmd}" if cmd in ("claude", "codex") else None,
        )
        caps = self._worker(stub_engine).capabilities
        assert "claude-code" in caps
        assert "codex" in caps

    def test_no_clis_present_no_agent_caps(self, stub_engine, monkeypatch) -> None:
        """Neither CLI on PATH → neither capability advertised, but core caps remain."""
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        caps = self._worker(stub_engine).capabilities
        assert "claude-code" not in caps
        assert "codex" not in caps
        # Core caps still present
        for c in ("code", "search", "memory", "test", "decompose", "review"):
            assert c in caps, f"{c} missing"

    def test_agent_caps_deduped(self, stub_engine, monkeypatch) -> None:
        """If 'claude-code' is somehow already in caps, CLI probe doesn't duplicate."""
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/claude" if cmd == "claude" else None)
        caps = self._worker(stub_engine).capabilities
        assert caps.count("claude-code") == 1
