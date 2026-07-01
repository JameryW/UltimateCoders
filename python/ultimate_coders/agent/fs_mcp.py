"""Filesystem MCP Server — exposes file edit tools for sandbox agents.

Run as a stdio MCP server:
    python -m ultimate_coders.agent.fs_mcp

Claude Code integration via mcp_configs:
    {"uc-fs": {
        "command": "python",
        "args": ["-m", "ultimate_coders.agent.fs_mcp"],
    }}

Or with explicit workspace root:
    {"uc-fs": {
        "command": "python",
        "args": ["-m", "ultimate_coders.agent.fs_mcp", "--workspace", "/workspace"],
    }}

Workspace isolation: all paths are resolved against the workspace root
(defaults to cwd / UC_WORKSPACE env) and rejected if they escape it. This is
the safety boundary — agents operate inside a git worktree and must not touch
files outside it.
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    # ponytail: mcp package is optional — only needed for stdio serve.
    # Tool handlers return TextContent-like objects; provide a minimal stand-in so the
    # pure-Python tool functions (_read_file/_write_file/_edit_file) work without mcp
    # installed (tests, non-MCP callers).
    Server = None  # type: ignore[assignment,misc]

    class TextContent:  # type: ignore[no-redef]
        __slots__ = ("type", "text")

        def __init__(self, type: str = "text", text: str = "") -> None:  # noqa: A002
            self.type = type
            self.text = text


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
    # ponytail: realpath collapses ../, symlinks — then prefix-check against root
    full = os.path.realpath(os.path.join(workspace, rel))
    root = os.path.realpath(workspace)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"path '{rel}' escapes workspace root")
    return full


def _create_server(workspace: str) -> object:
    """Create MCP server with file edit tools scoped to workspace."""
    if Server is None:
        print("mcp package not installed — pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("uc-fs")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="read_file",
                description=(
                    "Read a file from the worker workspace."
                    " Returns content with line numbers (cat -n format)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace root",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "1-based line to start from (optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max lines to read (optional, default 2000)",
                        },
                    },
                    "required": ["path"],
                },
            ),
            Tool(
                name="write_file",
                description=(
                    "Write (create or overwrite) a file in the worker workspace."
                    " Creates parent directories as needed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace root",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full file content to write",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            Tool(
                name="edit_file",
                description=(
                    "Replace a unique string occurrence in a workspace file."
                    " Fails if old_string is not found or not unique"
                    " (unless replace_all=true)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace root",
                        },
                        "old_string": {
                            "type": "string",
                            "description": (
                                "Exact text to replace"
                                " (must match file including indentation)"
                            ),
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Replace all occurrences instead of"
                                " requiring a unique match"
                            ),
                        },
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "read_file":
                return await _read_file(workspace, arguments)
            if name == "write_file":
                return await _write_file(workspace, arguments)
            if name == "edit_file":
                return await _edit_file(workspace, arguments)
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        except ValueError as e:
            # Safety boundary violation or bad input — surface to agent, don't crash server
            return [TextContent(type="text", text=f"Error: {e}")]
        except OSError as e:
            return [TextContent(type="text", text=f"IO error: {e}")]

    return server


async def _read_file(workspace: str, args: dict) -> list[TextContent]:
    full = _safe_path(workspace, args["path"])
    if not os.path.isfile(full):
        return [TextContent(type="text", text=f"File not found: {args['path']}")]
    offset = args.get("offset")
    limit = args.get("limit", 2000)
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    start = (offset - 1) if offset and offset > 0 else 0
    chunk = lines[start : start + limit] if limit else lines[start:]
    out = "".join(f"{i + start + 1}\t{line}" for i, line in enumerate(chunk))
    return [TextContent(type="text", text=out or "(empty file)")]


async def _write_file(workspace: str, args: dict) -> list[TextContent]:
    full = _safe_path(workspace, args["path"])
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    content = args["content"]
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return [TextContent(type="text", text=f"Wrote {len(content)} bytes to {args['path']}")]


async def _edit_file(workspace: str, args: dict) -> list[TextContent]:
    full = _safe_path(workspace, args["path"])
    if not os.path.isfile(full):
        return [TextContent(type="text", text=f"File not found: {args['path']}")]
    old = args["old_string"]
    new = args["new_string"]
    replace_all = args.get("replace_all", False)
    with open(full, encoding="utf-8", errors="replace") as f:
        text = f.read()
    count = text.count(old)
    if count == 0:
        return [TextContent(type="text", text=f"old_string not found in {args['path']}")]
    if count > 1 and not replace_all:
        msg = (
            f"old_string matches {count} times in {args['path']};"
            " set replace_all=true or narrow the match"
        )
        return [TextContent(type="text", text=msg)]
    if replace_all:
        new_text = text.replace(old, new)
    else:
        # Replace exactly the first (unique) occurrence
        new_text = text.replace(old, new, 1)
    with open(full, "w", encoding="utf-8") as f:
        f.write(new_text)
    return [
        TextContent(
            type="text",
            text=f"Replaced {count if replace_all else 1} occurrence(s) in {args['path']}",
        )
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="UC Filesystem MCP Server")
    parser.add_argument(
        "--workspace",
        default="",
        help="Workspace root (default: UC_WORKSPACE env or cwd)",
    )
    args = parser.parse_args()

    workspace = _resolve_workspace(args.workspace)
    server = _create_server(workspace)

    import asyncio

    asyncio.run(stdio_server(server).serve())


if __name__ == "__main__":
    main()
