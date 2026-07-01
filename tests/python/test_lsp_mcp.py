"""Unit tests for the uc-lsp MCP server (lsp_mcp.py).

Tests cover:
- All 5 LSP tools return TextContent with mocked multilspy results
- Graceful degradation when multilspy is not installed (LanguageServer is None)
- Workspace path isolation (paths outside workspace are rejected)
- 1-based → 0-based line/character conversion
- Unsupported language returns a clear hint, no crash
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
from ultimate_coders.agent.lsp_mcp import (
    TextContent,
    _detect_language,
    _document_symbols,
    _extract_symbol_at,
    _find_references,
    _go_to_definition,
    _hover,
    _safe_path,
    _workspace_symbol,
)
from ultimate_coders.agent.sandbox import SandboxConfig
from ultimate_coders.agent.worker import Worker

# ── Helpers ──────────────────────────────────────────────────────


def _make_location(uri: str, line: int, char: int) -> MagicMock:
    """Build a multilspy Location-like mock (0-based line/char)."""
    loc = MagicMock()
    loc.uri = uri
    start = MagicMock()
    start.line = line
    start.character = char
    rng = MagicMock()
    rng.start = start
    loc.range = rng
    return loc


def _make_hover(contents: str) -> MagicMock:
    h = MagicMock()
    h.contents = contents
    return h


def _make_symbol(name: str, kind: str) -> MagicMock:
    sym = MagicMock()
    sym.name = name
    sym.kind = kind
    sym.location = _make_location("file:///workspace/f.py", 1, 0)
    return sym


@pytest.fixture(autouse=True)
def _clear_ls_cache() -> None:
    """Clear the LanguageServer + CodegraphClient caches between tests."""
    from ultimate_coders.agent import lsp_mcp

    lsp_mcp._ls_cache.clear()
    lsp_mcp._cg_cache.clear()


@pytest.fixture
def mock_ls() -> MagicMock:
    """A mocked LanguageServer that returns canned LSP responses."""
    ls = MagicMock()
    ls.repository_root_path = "/workspace"
    ls.request_definition.return_value = [_make_location("file:///workspace/other.py", 10, 4)]
    ls.request_references.return_value = [
        _make_location("file:///workspace/a.py", 5, 0),
        _make_location("file:///workspace/b.py", 20, 8),
    ]
    ls.request_hover.return_value = _make_hover("def foo(x: int) -> str: ...")
    ls.request_document_symbols.return_value = (
        [_make_symbol("foo", "Function"), _make_symbol("Bar", "Class")],
        "Module\n├─foo (Function)\n└─Bar (Class)",
    )
    ls.request_workspace_symbol.return_value = [_make_symbol("foo", "Function")]
    return ls


# ── Path safety ─────────────────────────────────────────────────


class TestPathSafety:
    def test_safe_path_inside_workspace(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        assert _safe_path(ws, "src/main.py") == os.path.realpath(
            os.path.join(ws, "src/main.py")
        )

    def test_safe_path_rejects_escape(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            _safe_path(ws, "../../../etc/passwd")

    def test_safe_path_rejects_empty(self, tmp_path: object) -> None:
        with pytest.raises(ValueError, match="required"):
            _safe_path(str(tmp_path), "")


# ── Language detection ──────────────────────────────────────────


class TestDetectLanguage:
    def test_python_detected(self) -> None:
        assert _detect_language("foo.py") == "python"

    def test_rust_detected(self) -> None:
        assert _detect_language("foo.rs") == "rust"
        assert _detect_language("src/main.rs") == "rust"

    def test_typescript_detected(self) -> None:
        assert _detect_language("foo.ts") == "typescript"
        assert _detect_language("foo.tsx") == "typescript"
        assert _detect_language("foo.js") == "typescript"
        assert _detect_language("foo.jsx") == "typescript"

    def test_unsupported_language_returns_none(self) -> None:
        assert _detect_language("foo.go") is None
        assert _detect_language("foo.java") is None
        assert _detect_language("foo.c") is None

    def test_case_insensitive_extension(self) -> None:
        assert _detect_language("FOO.PY") == "python"
        assert _detect_language("FOO.RS") == "rust"
        assert _detect_language("FOO.TS") == "typescript"
        assert _detect_language("FOO.TSX") == "typescript"

    def test_unknown_extension_returns_none(self) -> None:
        assert _detect_language("README") is None
        assert _detect_language("foo.unknown") is None
        assert _detect_language("noext") is None


# ── Tool tests with mocked LanguageServer ───────────────────────


class TestLspTools:
    def test_go_to_definition_returns_locations(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        # Create a Python file so _safe_path resolves to a real file
        fpath = os.path.join(ws, "f.py")
        os.makedirs(ws, exist_ok=True)
        with open(fpath, "w") as f:
            f.write("def foo(): pass\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _go_to_definition(ws, {"path": "f.py", "line": 1, "character": 5})
            )
        assert isinstance(result[0], TextContent)
        assert "Definition:" in result[0].text
        # Verify 1-based → 0-based conversion: line=1 → 0, char=5 → 4
        mock_ls.request_definition.assert_called_once()
        call_args = mock_ls.request_definition.call_args
        # args are (rel_path, line, character)
        assert call_args[0][1] == 0  # line: 1 - 1 = 0
        assert call_args[0][2] == 4  # char: 5 - 1 = 4

    def test_go_to_definition_no_results(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("x = 1\n")
        mock_ls.request_definition.return_value = []

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _go_to_definition(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "No definitions found" in result[0].text

    def test_find_references_returns_count(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo()\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _find_references(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "References (2):" in result[0].text
        # Verify line/char conversion: 1,1 → 0,0
        call_args = mock_ls.request_references.call_args
        assert call_args[0][1] == 0
        assert call_args[0][2] == 0

    def test_find_references_no_results(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("x = 1\n")
        mock_ls.request_references.return_value = []

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _find_references(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "No references found" in result[0].text

    def test_hover_returns_contents(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _hover(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "def foo(x: int) -> str" in result[0].text
        call_args = mock_ls.request_hover.call_args
        assert call_args[0][1] == 0
        assert call_args[0][2] == 0

    def test_hover_none_returns_message(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("x\n")
        mock_ls.request_hover.return_value = None

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _hover(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "No hover information" in result[0].text

    def test_document_symbols_returns_tree(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("def foo(): pass\nclass Bar: pass\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _document_symbols(ws, {"path": "f.py"})
            )
        assert "foo" in result[0].text
        assert "Bar" in result[0].text

    def test_document_symbols_empty(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("\n")
        mock_ls.request_document_symbols.return_value = ([], "")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _document_symbols(ws, {"path": "f.py"})
            )
        assert "No symbols found" in result[0].text

    def test_workspace_symbol_returns_matches(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _workspace_symbol(ws, {"query": "foo"})
            )
        assert "Symbols (1):" in result[0].text
        assert "foo" in result[0].text
        mock_ls.request_workspace_symbol.assert_called_once_with("foo")

    def test_workspace_symbol_no_results(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        mock_ls.request_workspace_symbol.return_value = []

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=mock_ls):
            result = asyncio.run(
                _workspace_symbol(ws, {"query": "nonexistent"})
            )
        assert "No symbols matching" in result[0].text


# ── Rust / TypeScript multilspy path ─────────────────────────────


class TestRustTsMultilspyPath:
    """Rust (.rs) and TypeScript (.ts/.tsx/.js/.jsx) files should route
    through the multilspy LanguageServer path (not the codegraph fallback)
    when multilspy is available. The language string passed to
    _get_language_server must match multilspy's Language enum values."""

    def test_rust_definition_uses_multilspy(
        self, mock_ls: MagicMock, tmp_path: object
    ) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "lib.rs"), "w") as f:
            f.write("fn foo() {}\n")

        with patch(
            "ultimate_coders.agent.lsp_mcp._get_language_server",
            return_value=mock_ls,
        ) as mock_get_ls:
            result = asyncio.run(
                _go_to_definition(ws, {"path": "lib.rs", "line": 1, "character": 4})
            )
        # Not a fallback — no [codegraph fallback] prefix
        assert "[codegraph fallback]" not in result[0].text
        assert "Definition:" in result[0].text
        # multilspy request_definition was called (multilspy path)
        mock_ls.request_definition.assert_called_once()
        # The language server was requested with language="rust"
        mock_get_ls.assert_called_once_with(ws, "rust")

    def test_ts_definition_uses_multilspy(
        self, mock_ls: MagicMock, tmp_path: object
    ) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "mod.ts"), "w") as f:
            f.write("function foo() {}\n")

        with patch(
            "ultimate_coders.agent.lsp_mcp._get_language_server",
            return_value=mock_ls,
        ) as mock_get_ls:
            result = asyncio.run(
                _go_to_definition(ws, {"path": "mod.ts", "line": 1, "character": 10})
            )
        assert "[codegraph fallback]" not in result[0].text
        assert "Definition:" in result[0].text
        mock_ls.request_definition.assert_called_once()
        mock_get_ls.assert_called_once_with(ws, "typescript")

    def test_tsx_definition_uses_multilspy(
        self, mock_ls: MagicMock, tmp_path: object
    ) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "comp.tsx"), "w") as f:
            f.write("export const Foo = () => null;\n")

        with patch(
            "ultimate_coders.agent.lsp_mcp._get_language_server",
            return_value=mock_ls,
        ) as mock_get_ls:
            result = asyncio.run(
                _go_to_definition(ws, {"path": "comp.tsx", "line": 1, "character": 13})
            )
        assert "[codegraph fallback]" not in result[0].text
        mock_ls.request_definition.assert_called_once()
        mock_get_ls.assert_called_once_with(ws, "typescript")

    def test_rust_find_references_uses_multilspy(
        self, mock_ls: MagicMock, tmp_path: object
    ) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "lib.rs"), "w") as f:
            f.write("foo();\n")

        with patch(
            "ultimate_coders.agent.lsp_mcp._get_language_server",
            return_value=mock_ls,
        ) as mock_get_ls:
            result = asyncio.run(
                _find_references(ws, {"path": "lib.rs", "line": 1, "character": 1})
            )
        assert "[codegraph fallback]" not in result[0].text
        assert "References (2):" in result[0].text
        mock_ls.request_references.assert_called_once()
        mock_get_ls.assert_called_once_with(ws, "rust")

    def test_rust_hover_uses_multilspy(self, mock_ls: MagicMock, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "lib.rs"), "w") as f:
            f.write("let x = foo();\n")

        with patch(
            "ultimate_coders.agent.lsp_mcp._get_language_server",
            return_value=mock_ls,
        ):
            result = asyncio.run(
                _hover(ws, {"path": "lib.rs", "line": 1, "character": 9})
            )
        assert "[codegraph fallback]" not in result[0].text
        assert "def foo(x: int) -> str" in result[0].text

    def test_rust_doc_symbols_uses_multilspy(
        self, mock_ls: MagicMock, tmp_path: object
    ) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "lib.rs"), "w") as f:
            f.write("fn foo() {}\n")

        with patch(
            "ultimate_coders.agent.lsp_mcp._get_language_server",
            return_value=mock_ls,
        ):
            result = asyncio.run(
                _document_symbols(ws, {"path": "lib.rs"})
            )
        assert "[codegraph fallback]" not in result[0].text
        assert "foo" in result[0].text

    def test_rust_multilspy_unavailable_falls_back(self, tmp_path: object) -> None:
        """When multilspy fails to start for rust, fall back to codegraph."""
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "lib.rs"), "w") as f:
            f.write("foo()\n")
        cg = _make_codegraph_mock(
            search_results=[
                {"name": "foo", "kind": "function", "file_path": "def.rs", "start_line": 5}
            ]
        )
        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=None), \
             patch("ultimate_coders.agent.lsp_mcp._get_codegraph", return_value=cg):
            result = asyncio.run(
                _go_to_definition(ws, {"path": "lib.rs", "line": 1, "character": 1})
            )
        # Falls back to codegraph (multilspy unavailable)
        assert "[codegraph fallback]" in result[0].text
        assert "foo" in result[0].text


# ── Graceful degradation ─────────────────────────────────────────


class TestGracefulDegradation:
    def test_go_to_definition_multilspy_unavailable(self, tmp_path: object) -> None:
        """When multilspy is not installed / LanguageServer is None, tools degrade."""
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("x\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=None):
            result = asyncio.run(
                _go_to_definition(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "LSP unavailable" in result[0].text
        assert "multilspy not installed" in result[0].text

    def test_find_references_multilspy_unavailable(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("x\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=None):
            result = asyncio.run(
                _find_references(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "LSP unavailable" in result[0].text

    def test_hover_multilspy_unavailable(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("x\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=None):
            result = asyncio.run(
                _hover(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "LSP unavailable" in result[0].text

    def test_document_symbols_multilspy_unavailable(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("x\n")

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=None):
            result = asyncio.run(
                _document_symbols(ws, {"path": "f.py"})
            )
        assert "LSP unavailable" in result[0].text

    def test_workspace_symbol_multilspy_unavailable(self, tmp_path: object) -> None:
        ws = str(tmp_path)

        with patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=None):
            result = asyncio.run(
                _workspace_symbol(ws, {"query": "foo"})
            )
        assert "LSP unavailable" in result[0].text

    def test_unsupported_language_returns_hint(self, tmp_path: object) -> None:
        """Non-supported files return 'language not supported' without crashing."""
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.go"), "w") as f:
            f.write("func main() {}\n")

        result = asyncio.run(
            _go_to_definition(ws, {"path": "f.go", "line": 1, "character": 1})
        )
        assert "LSP unavailable" in result[0].text
        assert "language not supported" in result[0].text
        assert "f.go" in result[0].text


# ── Worker integration ──────────────────────────────────────────


class TestWorkerLspRegistration:
    def test_uc_lsp_auto_registered_with_engine(self, stub_engine: object) -> None:
        """Worker with engine auto-registers uc-lsp in mcp_configs."""
        worker = Worker(engine=stub_engine, sandbox_config=SandboxConfig())
        names = set()
        for entry in worker._sandbox_config.mcp_configs or []:
            if isinstance(entry, dict):
                names.update(entry.keys())
        assert "uc-lsp" in names

    def test_uc_lsp_alias_capability_derived(self, stub_engine: object) -> None:
        """Worker derives 'lsp' capability from mcp:uc-lsp alias."""
        worker = Worker(engine=stub_engine, sandbox_config=SandboxConfig())
        assert "lsp" in worker.capabilities

    def test_uc_lsp_registered_with_custom_mcp_configs(self, stub_engine: object) -> None:
        """uc-lsp is added even when caller supplies custom mcp_configs."""
        custom_cfg = SandboxConfig(
            mcp_configs=[{"my-tool": {"command": "echo", "args": ["hi"]}}]
        )
        worker = Worker(engine=stub_engine, sandbox_config=custom_cfg)
        names = set()
        for entry in worker._sandbox_config.mcp_configs or []:
            if isinstance(entry, dict):
                names.update(entry.keys())
        assert "uc-lsp" in names
        assert "uc-fs" in names
        assert "my-tool" in names


# ── Codegraph fallback ───────────────────────────────────────────


def _make_codegraph_mock(
    *, available: bool = True, search_results=None, callers=None, callees=None
) -> MagicMock:
    """Build a mocked CodegraphClient."""
    cg = MagicMock()
    cg.is_available.return_value = available
    cg.search.return_value = search_results or []
    cg.callers.return_value = callers or []
    cg.callees.return_value = callees or []
    return cg


def _fallback_patches(cg_mock: MagicMock | None):
    """Patch _get_language_server→None and _get_codegraph→cg_mock.

    Pass cg_mock=None to simulate codegraph also unavailable.
    """
    return (
        patch("ultimate_coders.agent.lsp_mcp._get_language_server", return_value=None),
        patch("ultimate_coders.agent.lsp_mcp._get_codegraph", return_value=cg_mock),
    )


class TestCodegraphFallback:
    """When multilspy is unavailable, tools fall back to codegraph."""

    def test_workspace_symbol_fallback_returns_search_results(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        cg = _make_codegraph_mock(
            search_results=[
                {"name": "foo", "kind": "function", "file_path": "a.py", "start_line": 10}
            ]
        )
        p1, p2 = _fallback_patches(cg)
        with p1, p2:
            result = asyncio.run(_workspace_symbol(ws, {"query": "foo"}))
        assert "[codegraph fallback]" in result[0].text
        assert "foo" in result[0].text
        assert "Symbols (1):" in result[0].text
        cg.search.assert_called_once_with("foo")

    def test_workspace_symbol_fallback_no_results(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        cg = _make_codegraph_mock(search_results=[])
        p1, p2 = _fallback_patches(cg)
        with p1, p2:
            result = asyncio.run(_workspace_symbol(ws, {"query": "bar"}))
        assert "[codegraph fallback]" in result[0].text
        assert "No symbols matching" in result[0].text

    def test_go_to_definition_fallback_returns_first_hit(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo()\n")
        cg = _make_codegraph_mock(
            search_results=[
                {"name": "foo", "kind": "function", "file_path": "def.py", "start_line": 5},
                {"name": "foo", "kind": "function", "file_path": "other.py", "start_line": 20},
            ]
        )
        p1, p2 = _fallback_patches(cg)
        with p1, p2:
            result = asyncio.run(
                _go_to_definition(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "[codegraph fallback]" in result[0].text
        assert "foo" in result[0].text
        assert "def.py:5" in result[0].text

    def test_find_references_fallback_unions_callers_and_search(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo()\n")
        cg = _make_codegraph_mock(
            search_results=[
                {"name": "foo", "kind": "function", "file_path": "def.py", "start_line": 5}
            ],
            callers=[
                {"name": "bar", "kind": "function", "file_path": "bar.py", "start_line": 12}
            ],
        )
        p1, p2 = _fallback_patches(cg)
        with p1, p2:
            result = asyncio.run(
                _find_references(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "[codegraph fallback]" in result[0].text
        assert "References (2):" in result[0].text
        assert "bar" in result[0].text
        assert "foo" in result[0].text

    def test_hover_fallback_returns_not_available_hint(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo()\n")
        cg = _make_codegraph_mock(
            search_results=[
                {"name": "foo", "kind": "function", "file_path": "def.py", "start_line": 5}
            ]
        )
        p1, p2 = _fallback_patches(cg)
        with p1, p2:
            result = asyncio.run(
                _hover(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "[codegraph fallback]" in result[0].text
        assert "不可用" in result[0].text
        assert "foo" in result[0].text
        assert "def.py:5" in result[0].text

    def test_document_symbols_fallback_returns_not_available_hint(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("def foo(): pass\n")
        cg = _make_codegraph_mock()
        p1, p2 = _fallback_patches(cg)
        with p1, p2:
            result = asyncio.run(_document_symbols(ws, {"path": "f.py"}))
        assert "[codegraph fallback]" in result[0].text
        assert "不可用" in result[0].text

    def test_go_to_definition_fallback_no_symbol_extracted(self, tmp_path: object) -> None:
        """When the file line has no identifier, fallback reports it can't map."""
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("123\n")  # no identifier
        cg = _make_codegraph_mock(search_results=[])
        p1, p2 = _fallback_patches(cg)
        with p1, p2:
            result = asyncio.run(
                _go_to_definition(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "[codegraph fallback]" in result[0].text
        assert "Could not extract" in result[0].text


class TestCodegraphUnavailableFallback:
    """When both multilspy AND codegraph are unavailable, return plain hint (no prefix)."""

    def test_workspace_symbol_codegraph_unavailable(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        p1, p2 = _fallback_patches(None)
        with p1, p2:
            result = asyncio.run(_workspace_symbol(ws, {"query": "foo"}))
        assert "[codegraph fallback]" not in result[0].text
        assert "LSP unavailable" in result[0].text

    def test_go_to_definition_codegraph_unavailable(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo()\n")
        p1, p2 = _fallback_patches(None)
        with p1, p2:
            result = asyncio.run(
                _go_to_definition(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "[codegraph fallback]" not in result[0].text
        assert "LSP unavailable" in result[0].text

    def test_hover_codegraph_unavailable(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo\n")
        p1, p2 = _fallback_patches(None)
        with p1, p2:
            result = asyncio.run(
                _hover(ws, {"path": "f.py", "line": 1, "character": 1})
            )
        assert "[codegraph fallback]" not in result[0].text
        assert "LSP unavailable" in result[0].text


class TestExtractSymbolAt:
    """Test the best-effort symbol extraction helper."""

    def test_extracts_symbol_at_position(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("result = process_data(input)\n")
        # "process_data" starts at char 10 (1-based)
        symbol = _extract_symbol_at(ws, "f.py", 1, 12)
        assert symbol == "process_data"

    def test_extracts_symbol_containing_position(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo_bar()\n")
        # char 3 is inside "foo_bar"
        symbol = _extract_symbol_at(ws, "f.py", 1, 3)
        assert symbol == "foo_bar"

    def test_returns_none_for_missing_file(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        assert _extract_symbol_at(ws, "nope.py", 1, 1) is None

    def test_returns_none_for_empty_line(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("\n\n")
        assert _extract_symbol_at(ws, "f.py", 1, 1) is None

    def test_returns_none_for_line_out_of_range(self, tmp_path: object) -> None:
        ws = str(tmp_path)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "f.py"), "w") as f:
            f.write("foo\n")
        assert _extract_symbol_at(ws, "f.py", 99, 1) is None
