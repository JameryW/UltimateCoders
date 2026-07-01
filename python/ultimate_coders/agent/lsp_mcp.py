"""LSP MCP Server — exposes real-time LSP symbol tools for sandbox agents.

Run as a stdio MCP server:
    python -m ultimate_coders.agent.lsp_mcp

Claude Code integration via mcp_configs:
    {"uc-lsp": {
        "command": "python",
        "args": ["-m", "ultimate_coders.agent.lsp_mcp"],
    }}

Or with explicit workspace root:
    {"uc-lsp": {
        "command": "python",
        "args": ["-m", "ultimate_coders.agent.lsp_mcp", "--workspace", "/workspace"],
    }}

Real-time: unlike codegraph (precomputed SQLite graph lagging writes ~1s),
this server drives a live language server (multilspy → jedi-language-server
for Python). Before each query the target file is re-read from disk and a
full-content didChange notification is sent, so the LSP sees the file as it
is right now — including edits the agent just made.

Graceful degradation: if multilspy is not installed, the language server
fails to start, or the language is not supported, every tool returns a
TextContent with a clear hint (e.g. "LSP unavailable: ...") instead of
crashing the server. Agents fall back to codegraph or read_file + grep.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    # ponytail: mcp package is optional — only needed for stdio serve.
    # Provide a minimal TextContent stand-in so pure-Python tool functions
    # work without mcp installed (tests, non-MCP callers).
    Server = None  # type: ignore[assignment,misc]

    class TextContent:  # type: ignore[no-redef]
        __slots__ = ("type", "text")

        def __init__(self, type: str = "text", text: str = "") -> None:  # noqa: A002
            self.type = type
            self.text = text


try:
    from multilspy.language_server import LanguageServer
    from multilspy.multilspy_config import MultilspyConfig
    from multilspy.multilspy_logger import MultilspyLogger
except ImportError:
    # ponytail: multilspy is an optional dep — not all workers need LSP.
    # When absent, all tools degrade to "LSP unavailable" messages.
    LanguageServer = None  # type: ignore[assignment,misc]
    MultilspyConfig = None  # type: ignore[assignment,misc]
    MultilspyLogger = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)

# File extensions → multilspy code_language values.
# Python is the MVP; other languages return "not supported" (fall back to
# codegraph / read_file). Add languages as their LSP servers land in the
# worker container.
_SUPPORTED_LANGS: dict[str, str] = {
    ".py": "python",
}

# Cache of (workspace, language) → started LanguageServer, kept alive for the
# MCP server's lifetime. multilspy startup is ~1s (jedi); reusing avoids
# per-request latency.
_ls_cache: dict[tuple[str, str], LanguageServer | None] = {}


def _resolve_workspace(arg: str) -> str:
    """Resolve the workspace root: explicit flag > UC_WORKSPACE env > cwd."""
    root = arg or os.environ.get("UC_WORKSPACE", "") or os.getcwd()
    return os.path.realpath(root)


def _safe_path(workspace: str, rel: str) -> str:
    """Resolve rel against workspace, returning the absolute path.

    Raises ValueError if the resolved path escapes the workspace root.
    """
    if not rel:
        raise ValueError("path is required")
    full = os.path.realpath(os.path.join(workspace, rel))
    root = os.path.realpath(workspace)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"path '{rel}' escapes workspace root")
    return full


def _detect_language(file_path: str) -> str | None:
    """Return the multilspy language code for a file, or None if unsupported."""
    _, ext = os.path.splitext(file_path)
    return _SUPPORTED_LANGS.get(ext.lower())


def _get_language_server(workspace: str, language: str) -> LanguageServer | None:
    """Get or lazily start a LanguageServer for (workspace, language).

    Returns None if multilspy is not installed or the server fails to start.
    Cached per (workspace, language) for the MCP server's lifetime.
    """
    if LanguageServer is None:
        return None
    cache_key = (workspace, language)
    if cache_key in _ls_cache:
        return _ls_cache[cache_key]
    ls: LanguageServer | None = None
    try:
        config = MultilspyConfig.from_dict({"code_language": language})
        log = MultilspyLogger()
        ls = LanguageServer.create(config, log, workspace)
        ls.start_server()  # blocks until initialized; ~1s for jedi
    except Exception as e:  # noqa: BLE001 — multilspy is pre-alpha, catch all
        logger.warning("multilspy LanguageServer start failed for %s: %s", language, e)
        ls = None
    _ls_cache[cache_key] = ls
    return ls


def _sync_file(ls: LanguageServer, abs_path: str) -> None:
    """Ensure the LSP sees the current on-disk file state before a query.

    multilspy's request_* methods call open_file() internally, which reads the
    file from disk on first open (did_open with fresh content). But if the file
    was opened by a prior query, multilspy reuses the cached buffer and does NOT
    re-read — so edits between queries would be missed.

    Fix: close any stale buffer so the next open_file() re-reads from disk.
    Full-content refresh (not incremental) per prd — simple and correct.
    """
    # Build the file:// URI the same way multilspy does (pathlib.Path.as_uri)
    import pathlib

    try:
        uri = pathlib.Path(abs_path).as_uri()
    except (ValueError, OSError):
        return
    # Force-close any cached buffer so request_* re-opens from disk.
    try:
        buffers = getattr(ls, "open_file_buffers", None)
        if buffers is not None and uri in buffers:
            from multilspy.lsp_protocol_handler.lsp_constants import LSPConstants

            ls.server.notify.did_close_text_document(
                {LSPConstants.TEXT_DOCUMENT: {LSPConstants.URI: uri}}
            )
            del buffers[uri]
    except Exception:  # noqa: BLE001 — best-effort sync; request still works
        pass


def _format_location(loc: object) -> str:
    """Format a multilspy Location-like object as a concise string."""
    # multilspy Location: {uri, range: {start: {line, character}, end: ...}}
    def _get(obj: object, attr: str) -> object:
        if isinstance(obj, dict):
            return obj.get(attr)
        return getattr(obj, attr, None)

    uri = _get(loc, "uri") or "?"
    rng = _get(loc, "range")
    line, char = "?", "?"
    if rng is not None:
        start = _get(rng, "start")
        if start is not None:
            line = _get(start, "line") or "?"
            char = _get(start, "character") or "?"
    # LSP lines/chars are 0-based; convert to 1-based for human display
    try:
        line = int(line) + 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    try:
        char = int(char) + 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    return f"{uri}:{line}:{char}"


def _create_server(workspace: str) -> object:
    """Create MCP server with LSP symbol tools scoped to workspace."""
    if Server is None:
        print("mcp package not installed — pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("uc-lsp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="go_to_definition",
                description=(
                    "Find where a symbol is defined (real-time LSP)."
                    " Returns definition locations for the symbol at the"
                    " given position. Reflects the current on-disk file"
                    " state — use instead of codegraph for live edits."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace root",
                        },
                        "line": {
                            "type": "integer",
                            "description": "1-based line number",
                        },
                        "character": {
                            "type": "integer",
                            "description": "1-based character offset (column)",
                        },
                    },
                    "required": ["path", "line", "character"],
                },
            ),
            Tool(
                name="find_references",
                description=(
                    "Find all references to the symbol at the given position"
                    " (real-time LSP, cross-file). Reflects current file state."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace root",
                        },
                        "line": {
                            "type": "integer",
                            "description": "1-based line number",
                        },
                        "character": {
                            "type": "integer",
                            "description": "1-based character offset (column)",
                        },
                    },
                    "required": ["path", "line", "character"],
                },
            ),
            Tool(
                name="hover",
                description=(
                    "Get hover/type info for the symbol at the given position"
                    " (real-time LSP). Returns type signature and docstring."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace root",
                        },
                        "line": {
                            "type": "integer",
                            "description": "1-based line number",
                        },
                        "character": {
                            "type": "integer",
                            "description": "1-based character offset (column)",
                        },
                    },
                    "required": ["path", "line", "character"],
                },
            ),
            Tool(
                name="document_symbols",
                description=(
                    "List all symbols (functions, classes, methods) in a file"
                    " (real-time LSP). Reflects current on-disk file state."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace root",
                        },
                    },
                    "required": ["path"],
                },
            ),
            Tool(
                name="workspace_symbol",
                description=(
                    "Search for symbols across the entire workspace by name"
                    " (real-time LSP). Use for 'where is X defined' queries."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Symbol name (or prefix) to search for",
                        },
                    },
                    "required": ["query"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "go_to_definition":
                return await _go_to_definition(workspace, arguments)
            if name == "find_references":
                return await _find_references(workspace, arguments)
            if name == "hover":
                return await _hover(workspace, arguments)
            if name == "document_symbols":
                return await _document_symbols(workspace, arguments)
            if name == "workspace_symbol":
                return await _workspace_symbol(workspace, arguments)
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:  # noqa: BLE001 — never crash the MCP server
            logger.warning("uc-lsp tool '%s' failed: %s", name, e)
            return [TextContent(type="text", text=f"LSP error: {e}")]

    return server


def _unavailable_msg(reason: str) -> str:
    """Build a graceful-degradation message."""
    return f"LSP unavailable: {reason}. Use codegraph or read_file + grep instead."


def _unavailable(reason: str) -> list[TextContent]:
    """Return a single TextContent with an LSP-unavailable message."""
    return [TextContent(type="text", text=_unavailable_msg(reason))]


_MULTILSP_DOWN = "multilspy not installed or LSP server failed to start"


def _resolve_ls(workspace: str, path: str) -> tuple[LanguageServer | None, str | None, str | None]:
    """Resolve the LanguageServer + rel path for a file, or return a degradation reason.

    Returns (ls, rel_path, reason):
    - If language unsupported: (None, None, "language not supported for <path>")
    - If multilspy unavailable: (None, None, _MULTILSP_DOWN)
    - On success: (ls, rel_path, None)
    """
    abs_path = _safe_path(workspace, path)
    lang = _detect_language(abs_path)
    if lang is None:
        return None, None, f"language not supported for {path}"
    ls = _get_language_server(workspace, lang)
    if ls is None:
        return None, None, _MULTILSP_DOWN
    rel = os.path.relpath(abs_path, workspace)
    return ls, rel, None


async def _go_to_definition(workspace: str, args: dict) -> list[TextContent]:
    ls, rel, reason = _resolve_ls(workspace, args["path"])
    if reason is not None:
        return _unavailable(reason)
    assert ls is not None and rel is not None
    # 1-based → 0-based
    line = int(args["line"]) - 1
    char = int(args["character"]) - 1
    _sync_file(ls, os.path.join(workspace, rel))
    result = await asyncio.to_thread(ls.request_definition, rel, line, char)
    if not result:
        return [TextContent(type="text", text="No definitions found.")]
    lines = [f"Definition: {_format_location(loc)}" for loc in result]
    return [TextContent(type="text", text="\n".join(lines))]


async def _find_references(workspace: str, args: dict) -> list[TextContent]:
    ls, rel, reason = _resolve_ls(workspace, args["path"])
    if reason is not None:
        return _unavailable(reason)
    assert ls is not None and rel is not None
    line = int(args["line"]) - 1
    char = int(args["character"]) - 1
    _sync_file(ls, os.path.join(workspace, rel))
    result = await asyncio.to_thread(ls.request_references, rel, line, char)
    if not result:
        return [TextContent(type="text", text="No references found.")]
    lines = [f"  {_format_location(loc)}" for loc in result]
    out = f"References ({len(result)}):\n" + "\n".join(lines)
    return [TextContent(type="text", text=out)]


async def _hover(workspace: str, args: dict) -> list[TextContent]:
    ls, rel, reason = _resolve_ls(workspace, args["path"])
    if reason is not None:
        return _unavailable(reason)
    assert ls is not None and rel is not None
    line = int(args["line"]) - 1
    char = int(args["character"]) - 1
    _sync_file(ls, os.path.join(workspace, rel))
    result = await asyncio.to_thread(ls.request_hover, rel, line, char)
    if result is None:
        return [TextContent(type="text", text="No hover information available.")]
    # multilspy Hover has .contents (str) or dict-like structure
    contents = getattr(result, "contents", None)
    if contents is None and isinstance(result, dict):
        contents = result.get("contents", str(result))
    if contents is None:
        contents = str(result)
    return [TextContent(type="text", text=str(contents))]


async def _document_symbols(workspace: str, args: dict) -> list[TextContent]:
    ls, rel, reason = _resolve_ls(workspace, args["path"])
    if reason is not None:
        return _unavailable(reason)
    assert ls is not None and rel is not None
    _sync_file(ls, os.path.join(workspace, rel))
    symbols, tree_repr = await asyncio.to_thread(ls.request_document_symbols, rel)
    if not symbols:
        return [TextContent(type="text", text="No symbols found in file.")]
    lines = [str(tree_repr) if tree_repr else ""]
    for sym in symbols:
        name = getattr(sym, "name", "?")
        kind = getattr(sym, "kind", "?")
        loc = getattr(sym, "location", None)
        loc_str = _format_location(loc) if loc else "?"
        lines.append(f"  {kind} {name} @ {loc_str}")
    return [TextContent(type="text", text="\n".join(lines).strip())]


async def _workspace_symbol(workspace: str, args: dict) -> list[TextContent]:
    query = args["query"]
    # workspace_symbol needs a language server — use python as default
    ls = _get_language_server(workspace, "python")
    if ls is None:
        return _unavailable(_MULTILSP_DOWN)
    result = await asyncio.to_thread(ls.request_workspace_symbol, query)
    if not result:
        return [TextContent(type="text", text=f"No symbols matching '{query}'.")]
    lines = [f"  {getattr(s, 'name', '?')} ({getattr(s, 'kind', '?')})" for s in result]
    return [TextContent(type="text", text=f"Symbols ({len(result)}):\n" + "\n".join(lines))]


def main() -> None:
    parser = argparse.ArgumentParser(description="UC LSP MCP Server")
    parser.add_argument(
        "--workspace",
        default="",
        help="Workspace root (default: UC_WORKSPACE env or cwd)",
    )
    args = parser.parse_args()

    workspace = _resolve_workspace(args.workspace)
    server = _create_server(workspace)

    asyncio.run(stdio_server(server).serve())


if __name__ == "__main__":
    main()
