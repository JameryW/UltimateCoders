"""Engine MCP Server — exposes search and memory as MCP tools for sandbox agents.

Run as a stdio MCP server:
    python -m ultimate_coders.agent.engine_mcp

Claude Code integration via mcp_configs:
    {"uc-engine": {"command": "python", "args": ["-m", "ultimate_coders.agent.engine_mcp"]}}

Or with explicit gRPC endpoint:
    {"uc-engine": {"command": "python", "args": ["-m", "ultimate_coders.agent.engine_mcp", "--grpc-endpoint", "http://gateway:50051"]}}
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    # ponytail: mcp package is optional — only needed for sandbox agent tools
    Server = None  # type: ignore[assignment,misc]

from ultimate_coders.engine import Engine


def _create_server(engine: Engine) -> object:
    """Create MCP server with search and memory tools."""
    if Server is None:
        print("mcp package not installed — pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("uc-engine")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="search_code",
                description="Search across indexed repositories for code. "
                "Supports text, semantic, and AST search modes.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (natural language or code pattern)"},
                        "modes": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["text", "semantic", "ast", "hybrid"]},
                            "default": ["hybrid"],
                            "description": "Search modes to use",
                        },
                        "project_id": {"type": "string", "description": "Project scope (optional)"},
                        "max_results": {"type": "integer", "default": 10, "description": "Max results"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="read_memory",
                description="Read project-scoped or global shared memory. "
                "Accessible across Workers via Gateway.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Memory key"},
                        "project_id": {"type": "string", "description": "Project scope (optional, global if empty)"},
                    },
                    "required": ["key"],
                },
            ),
            Tool(
                name="write_memory",
                description="Write project-scoped or global shared memory. "
                "Accessible across Workers via Gateway.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Memory key"},
                        "content": {"type": "string", "description": "Content to store"},
                        "project_id": {"type": "string", "description": "Project scope (optional, global if empty)"},
                        "content_type": {
                            "type": "string",
                            "default": "text",
                            "enum": ["text", "structured", "code", "diff", "reference"],
                        },
                        "importance": {"type": "number", "default": 0.7},
                    },
                    "required": ["key", "content"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "search_code":
            return await _search_code(engine, arguments)
        if name == "read_memory":
            return await _read_memory(engine, arguments)
        if name == "write_memory":
            return await _write_memory(engine, arguments)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return server


async def _search_code(engine: Engine, args: dict) -> list[TextContent]:
    from ultimate_coders.search.query import SearchQuery

    query = args["query"]
    modes = args.get("modes", ["hybrid"])
    project_id = args.get("project_id", "")
    max_results = args.get("max_results", 10)

    sq = SearchQuery(query).with_modes(modes).limit(max_results)
    if project_id:
        sq.in_repos([project_id])
    else:
        sq.in_all_repos(engine)

    result = engine.search(sq)
    items = getattr(result, "items", result) if result else []
    if not items:
        return [TextContent(type="text", text="No results found.")]

    lines = []
    for r in items[:max_results]:
        repo = getattr(r, "repo_id", "?")
        path = getattr(r, "file_path", "?")
        snippet = getattr(r, "content_snippet", "")
        score = getattr(r, "score", 0.0)
        lines.append(f"[{repo}] {path} (score={score:.2f})")
        if snippet:
            lines.append(f"  {snippet[:200]}")
    return [TextContent(type="text", text="\n".join(lines))]


async def _read_memory(engine: Engine, args: dict) -> list[TextContent]:
    key = args["key"]
    project_id = args.get("project_id", "")
    scope = "project" if project_id else "global"
    result = engine.read_memory(key_scope=scope, key=key, project_id=project_id or None)
    if result is None:
        return [TextContent(type="text", text=f"Memory key '{key}' not found.")]
    content = getattr(result, "content", str(result))
    return [TextContent(type="text", text=str(content))]


async def _write_memory(engine: Engine, args: dict) -> list[TextContent]:
    key = args["key"]
    content = args["content"]
    project_id = args.get("project_id", "")
    content_type = args.get("content_type", "text")
    importance = args.get("importance", 0.7)
    scope = "project" if project_id else "global"
    result = engine.write_memory(
        key_scope=scope, key=key, content=content,
        content_type=content_type, source_agent="mcp:uc-engine",
        importance=importance, project_id=project_id or None,
    )
    status = "ok" if result else "failed"
    return [TextContent(type="text", text=f"Memory '{key}' written ({status}).")]


def main() -> None:
    parser = argparse.ArgumentParser(description="UC Engine MCP Server")
    parser.add_argument("--grpc-endpoint", default="", help="gRPC Gateway endpoint")
    args = parser.parse_args()

    if args.grpc_endpoint:
        engine = Engine(mode="grpc", grpc_endpoint=args.grpc_endpoint, fallback_mode="auto")
    else:
        engine = Engine(mode="local")

    server = _create_server(engine)

    import asyncio
    asyncio.run(stdio_server(server).serve())


if __name__ == "__main__":
    main()
