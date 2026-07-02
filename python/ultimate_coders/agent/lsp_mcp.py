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
fails to start, or the language is not supported, every tool automatically
falls back to codegraph (same-process CodegraphClient reading
.codegraph/codegraph.db) with best-effort semantic mapping. Results are
prefixed "[codegraph fallback]" so the agent knows precision may differ
from a real LSP. If codegraph is also unavailable, a plain "LSP
unavailable" hint is returned (no crash).

Fallback semantic mapping (best-effort, NOT LSP-precision):
- workspace_symbol(query) → codegraph.search(query) — direct
- find_references(path,line,char) → extract symbol name from file,
  then codegraph.callers(symbol) + codegraph.search(symbol) union
- go_to_definition(path,line,char) → extract symbol name, codegraph.search
  returns first definition location
- hover → codegraph has NO equivalent → returns "not available" hint +
  symbol location if search found one
- document_symbols → codegraph is a cross-repo call graph, not a per-file
  tree → returns "not available" hint + alternative suggestion
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ultimate_coders.agent.codegraph import CodegraphClient

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
# multilspy's Language enum (str, Enum) has matching values: "python",
# "rust", "typescript". Adding a mapping here makes _get_language_server
# construct the right LanguageServer (RustAnalyzer / TypeScriptLanguageServer);
# multilspy handles binary discovery/startup, and if it fails the existing
# codegraph fallback still kicks in (PR #204).
_SUPPORTED_LANGS: dict[str, str] = {
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
}

# Cache of (workspace, language) → started LanguageServer, kept alive for the
# MCP server's lifetime. multilspy startup is ~1s (jedi); reusing avoids
# per-request latency.
_ls_cache: dict[tuple[str, str], LanguageServer | None] = {}

# Cache of workspace → CodegraphClient, kept alive for the MCP server's
# lifetime. CodegraphClient opens a SQLite connection lazily; reusing avoids
# re-checking .codegraph/codegraph.db on every fallback call.
_cg_cache: dict[str, CodegraphClient | None] = {}


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


def _get_codegraph(workspace: str) -> CodegraphClient | None:
    """Get or lazily construct a CodegraphClient for the workspace.

    Returns None if codegraph is not available (.codegraph/codegraph.db
    does not exist). Cached per workspace for the MCP server's lifetime.

    When ``UC_GRPC_ENDPOINT`` is set, the client is constructed with a
    gateway Engine so ``search`` queries the shared Postgres-backed AST
    index first (unified cross-worker symbol view), falling back to the
    local SQLite on miss.
    """
    if workspace in _cg_cache:
        return _cg_cache[workspace]
    cg: CodegraphClient | None = None
    try:
        from ultimate_coders.agent.codegraph import CodegraphClient

        engine = _maybe_gateway_engine()
        repo_id = os.environ.get("UC_REPO_ID", "")
        client = CodegraphClient(workspace, engine=engine, repo_id=repo_id)
        # Keep the client even if the local DB is missing — when an engine is
        # configured, search can still hit the gateway. Only return None when
        # there's neither a local DB nor a gateway engine.
        if client.is_available() or engine is not None:
            cg = client
        else:
            cg = None
    except Exception:  # noqa: BLE001 — codegraph import/construction is best-effort
        logger.debug("CodegraphClient construction failed for %s", workspace, exc_info=True)
        cg = None
    _cg_cache[workspace] = cg
    return cg


# Cached gateway Engine for unified codegraph search. Lazily constructed from
# UC_GRPC_ENDPOINT; None when unset or construction fails.
_gateway_engine_cache: object | None = None
_gateway_engine_tried: bool = False


def _maybe_gateway_engine() -> object | None:
    """Construct a gateway gRPC Engine from UC_GRPC_ENDPOINT, cached.

    Returns None if the env var is unset or construction fails. Best-effort —
    codegraph falls back to local SQLite when this is None.
    """
    global _gateway_engine_cache, _gateway_engine_tried
    if _gateway_engine_tried:
        return _gateway_engine_cache
    _gateway_engine_tried = True
    endpoint = os.environ.get("UC_GRPC_ENDPOINT", "")
    if not endpoint:
        return None
    try:
        from ultimate_coders.engine import Engine

        _gateway_engine_cache = Engine(mode="grpc", grpc_endpoint=endpoint)
        logger.info("codegraph unified search enabled via gateway: %s", endpoint)
    except Exception:  # noqa: BLE001
        logger.debug(
            "Failed to construct gateway engine for codegraph: %s",
            endpoint,
            exc_info=True,
        )
        _gateway_engine_cache = None
    return _gateway_engine_cache


# Regex to grab an identifier token at/around a character position.
# Ponytail: simple regex, no parser/tree-sitter.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _extract_symbol_at(workspace: str, path: str, line: int, char: int) -> str | None:
    """Best-effort extract the identifier token at line:char in a file.

    Args:
        workspace: Workspace root (absolute).
        path: File path relative to workspace.
        line: 1-based line number.
        char: 1-based character offset.

    Returns the identifier string, or None if the file can't be read or no
    identifier is found at/around the position.
    """
    try:
        abs_path = _safe_path(workspace, path)
    except ValueError:
        return None
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    idx = line - 1
    if idx < 0 or idx >= len(lines):
        return None
    text = lines[idx]
    # 0-based char offset for scanning
    pos = max(0, char - 1)
    if pos > len(text):
        pos = len(text)
    # Find the identifier token containing or nearest to pos
    best: str | None = None
    for m in _IDENT_RE.finditer(text):
        start, end = m.start(), m.end()
        if start <= pos < end:
            return m.group()
        if pos < start:
            # Nearest token to the right
            best = best or m.group()
    # If pos was past all tokens, take the last one as best-effort
    if best is not None:
        return best
    matches = _IDENT_RE.findall(text)
    return matches[-1] if matches else None


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
    """Build a graceful-degradation message (used when codegraph is also unavailable)."""
    return f"LSP unavailable: {reason}. Use codegraph or read_file + grep instead."


def _unavailable(reason: str) -> list[TextContent]:
    """Return a single TextContent with an LSP-unavailable message."""
    return [TextContent(type="text", text=_unavailable_msg(reason))]


def _fallback_text(body: str) -> list[TextContent]:
    """Wrap a fallback result body with the [codegraph fallback] source prefix."""
    return [TextContent(type="text", text=f"[codegraph fallback] {body}")]


_MULTILSP_DOWN = "multilspy not installed or LSP server failed to start"


# ── Codegraph fallback implementations ────────────────────────────


def _fmt_cg_row(r: dict) -> str:
    """Format a codegraph result dict as a single result row."""
    name = r.get("name", "?")
    kind = r.get("kind", "?")
    fp = r.get("file_path", "?")
    sl = r.get("start_line", "?")
    return f"  {name} ({kind}) @ {fp}:{sl}"


def _fallback_workspace_symbol(
    workspace: str, query: str, reason: str = _MULTILSP_DOWN
) -> list[TextContent]:
    """Fallback workspace_symbol via codegraph.search()."""
    cg = _get_codegraph(workspace)
    if cg is None:
        return _unavailable(reason)
    results = cg.search(query)
    if not results:
        return _fallback_text(f"No symbols matching '{query}' in codegraph.")
    lines = [_fmt_cg_row(r) for r in results]
    return _fallback_text(f"Symbols ({len(results)}):\n" + "\n".join(lines))


def _fallback_go_to_definition(
    workspace: str, path: str, line: int, char: int, reason: str = _MULTILSP_DOWN
) -> list[TextContent]:
    """Fallback go_to_definition: extract symbol, return first codegraph.search hit."""
    cg = _get_codegraph(workspace)
    if cg is None:
        return _unavailable(reason)
    symbol = _extract_symbol_at(workspace, path, line, char)
    if symbol is None:
        return _fallback_text(
            f"Could not extract symbol at {path}:{line}:{char}"
            " — cannot map to codegraph. Use read_file to inspect."
        )
    results = cg.search(symbol)
    if not results:
        return _fallback_text(f"No definition found for '{symbol}' in codegraph.")
    r = results[0]
    loc = f"{r.get('file_path', '?')}:{r.get('start_line', '?')}"
    return _fallback_text(f"Definition of '{symbol}': {loc}")


def _fallback_find_references(
    workspace: str, path: str, line: int, char: int, reason: str = _MULTILSP_DOWN
) -> list[TextContent]:
    """Fallback find_references: extract symbol, union callers + search results."""
    cg = _get_codegraph(workspace)
    if cg is None:
        return _unavailable(reason)
    symbol = _extract_symbol_at(workspace, path, line, char)
    if symbol is None:
        return _fallback_text(
            f"Could not extract symbol at {path}:{line}:{char}"
            " — cannot map to codegraph. Use read_file to inspect."
        )
    callers = cg.callers(symbol)
    search_hits = cg.search(symbol)
    # Union: callers give call-sites, search gives definition + references
    seen: set[tuple[str, int]] = set()
    refs: list[str] = []
    for c in callers:
        key = (c.get("file_path", ""), c.get("start_line", 0))
        if key not in seen:
            seen.add(key)
            refs.append(_fmt_cg_row(c))
    for r in search_hits:
        key = (r.get("file_path", ""), r.get("start_line", 0))
        if key not in seen:
            seen.add(key)
            refs.append(_fmt_cg_row(r))
    if not refs:
        return _fallback_text(f"No references found for '{symbol}' in codegraph.")
    return _fallback_text(f"References ({len(refs)}):\n" + "\n".join(refs))


def _fallback_hover(
    workspace: str, path: str, line: int, char: int, reason: str = _MULTILSP_DOWN
) -> list[TextContent]:
    """Fallback hover: codegraph has no hover/type equivalent — return a hint."""
    cg = _get_codegraph(workspace)
    if cg is None:
        return _unavailable(reason)
    symbol = _extract_symbol_at(workspace, path, line, char)
    location_hint = ""
    if symbol is not None:
        results = cg.search(symbol)
        if results:
            r = results[0]
            loc = f"{r.get('file_path', '?')}:{r.get('start_line', '?')}"
            location_hint = f" Symbol '{symbol}' found at {loc}."
    return _fallback_text(
        "hover 语义不可用（codegraph 无类型/文档信息），"
        f"建议 read_file 查看上下文。{location_hint}"
    )


def _fallback_document_symbols(
    workspace: str, path: str, reason: str = _MULTILSP_DOWN
) -> list[TextContent]:
    """Fallback document_symbols: codegraph is a cross-repo graph, not a per-file tree."""
    cg = _get_codegraph(workspace)
    if cg is None:
        return _unavailable(reason)
    return _fallback_text(
        "document_symbols 不可用（codegraph 是跨仓库调用图非 per-file 树），"
        "建议 read_file 或 workspace_symbol 按文件路径过滤。"
    )


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
        return _fallback_go_to_definition(
            workspace, args["path"], int(args["line"]), int(args["character"]), reason
        )
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
        return _fallback_find_references(
            workspace, args["path"], int(args["line"]), int(args["character"]), reason
        )
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
        return _fallback_hover(
            workspace, args["path"], int(args["line"]), int(args["character"]), reason
        )
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
        return _fallback_document_symbols(workspace, args["path"], reason)
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
        return _fallback_workspace_symbol(workspace, query)
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
