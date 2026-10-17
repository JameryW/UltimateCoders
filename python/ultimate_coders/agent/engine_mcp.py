"""Engine MCP Server — exposes search and memory as MCP tools for sandbox agents.

Run as a stdio MCP server:
    python -m ultimate_coders.agent.engine_mcp

Claude Code integration via mcp_configs:
    {"uc-engine": {
        "command": "python",
        "args": ["-m", "ultimate_coders.agent.engine_mcp"],
    }}

Or with explicit gRPC endpoint:
    {"uc-engine": {
        "command": "python",
        "args": [
            "-m", "ultimate_coders.agent.engine_mcp",
            "--grpc-endpoint", "http://gateway:50051",
        ],
    }}
"""

from __future__ import annotations

import argparse
import sys

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    # ponytail: mcp package is optional — only needed for sandbox agent tools.
    # The module contract must stay total: every name the handlers and
    # descriptors reference is defined either way (None when the package is
    # missing), so a function-level caller never hits a NameError — the gates
    # below hide the tools instead.
    Server = None  # type: ignore[assignment,misc]
    Tool = None  # type: ignore[assignment,misc]
    TextContent = None  # type: ignore[assignment,misc]

from ultimate_coders.agent.hy_memory import CodingMemory, hy_memory_available
from ultimate_coders.engine import Engine

#: Lazily-built coding-memory backend for the hy-memory MCP tools. None
#: until the first tool call; stays None (tools hidden) when the optional
#: hy-memory package is not installed.
_MEMORY_BACKEND: CodingMemory | None = None


def _memory_backend() -> CodingMemory | None:
    """Build (once) the CodingMemory for this MCP server process.

    Isolation coordinates come from the environment, with per-call
    overrides in the tool arguments. Returns None when hy-memory is not
    installed — the tools are then hidden from list_tools().
    """
    global _MEMORY_BACKEND
    if _MEMORY_BACKEND is not None or not hy_memory_available():
        return _MEMORY_BACKEND
    import os

    _MEMORY_BACKEND = CodingMemory(
        user_id=os.environ.get("UC_MEMORY_USER_ID", "default"),
        workspace_id=os.environ.get("UC_MEMORY_WORKSPACE") or None,
        branch=os.environ.get("UC_MEMORY_BRANCH") or None,
    )
    return _MEMORY_BACKEND


def _memory_tools() -> list:
    """Tool descriptors for the hy-memory backend (empty when unavailable).

    Hidden both when the optional ``hy-memory`` package is missing AND when
    the optional ``mcp`` package is missing (``Server`` is None then, and
    ``Tool`` / ``TextContent`` are None too — the import guard defines them
    either way so callers never hit a NameError).
    """
    if not hy_memory_available() or Server is None:
        return []
    return [
        Tool(
            name="recall_coding_memory",
            description="Recall coding memory for this repository: extracted "
            "facts, reusable experiences and the code version-evolution "
            "chain. Use before starting a task that resembles earlier work.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to recall (natural language)",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "Repo scope, e.g. github.com/org/repo "
                        "(defaults to UC_MEMORY_WORKSPACE)",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch scope (defaults to UC_MEMORY_BRANCH)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 5,
                        "description": "Max facts/experiences",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="remember_coding_memory",
            description="Record a completed task trace (messages including "
            "tool calls) into coding memory. The backend extracts the fact "
            "of what was done plus reusable experiences, and appends real "
            "diffs to the version-evolution chain.",
            inputSchema={
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "OpenAI-format messages including tool calls",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "Repo scope (defaults to UC_MEMORY_WORKSPACE)",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch scope (defaults to UC_MEMORY_BRANCH)",
                    },
                },
                "required": ["messages"],
            },
        ),
    ]


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
                        "query": {
                            "type": "string",
                            "description": ("Search query (natural language or code pattern)"),
                        },
                        "modes": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "text",
                                    "semantic",
                                    "ast",
                                    "hybrid",
                                ],
                            },
                            "default": ["hybrid"],
                            "description": "Search modes to use",
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Project scope (optional)",
                        },
                        "max_results": {
                            "type": "integer",
                            "default": 10,
                            "description": "Max results",
                        },
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
                        "key": {
                            "type": "string",
                            "description": "Memory key",
                        },
                        "project_id": {
                            "type": "string",
                            "description": ("Project scope (optional, global if empty)"),
                        },
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
                        "key": {
                            "type": "string",
                            "description": "Memory key",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to store",
                        },
                        "project_id": {
                            "type": "string",
                            "description": ("Project scope (optional, global if empty)"),
                        },
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
            *_memory_tools(),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "search_code":
            return await _search_code(engine, arguments)
        if name == "recall_coding_memory":
            return await _recall_coding_memory(engine, arguments)
        if name == "remember_coding_memory":
            return await _remember_coding_memory(engine, arguments)
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
        key_scope=scope,
        key=key,
        content=content,
        content_type=content_type,
        source_agent="mcp:uc-engine",
        importance=importance,
        project_id=project_id or None,
    )
    status = "ok" if result else "failed"
    return [TextContent(type="text", text=f"Memory '{key}' written ({status}).")]


async def _recall_coding_memory(engine: Engine, args: dict) -> list[TextContent]:
    backend = _memory_backend()
    if backend is None:
        return [TextContent(type="text", text="hy-memory backend unavailable.")]
    query = args["query"]
    workspace_id = args.get("workspace_id") or backend.workspace_id
    branch = args.get("branch") or backend.branch
    limit = int(args.get("limit") or 5)
    scoped = CodingMemory(
        user_id=backend.user_id,
        workspace_id=workspace_id,
        branch=branch,
        client=backend.client,
    )
    hits = scoped.recall(query, limit=limit)
    if not hits["ok"]:
        return [TextContent(type="text", text=f"Coding memory recall failed: {hits['error']}")]

    lines: list[str] = []
    for fact in hits["facts"]:
        statement = fact.get("statement") or fact.get("content") or str(fact)
        lines.append(f"[fact] {statement}")
    for exp in hits["experiences"]:
        task = exp.get("task") or exp.get("content") or str(exp)
        solution = exp.get("solution", "")
        lines.append(f"[experience] {task}" + (f" -> {solution}" if solution else ""))
    chain = hits.get("chain_block")
    if chain:
        text = chain.get("text") or chain.get("summary") or str(chain)
        lines.append(f"[chain] {text}")
    if not lines:
        return [TextContent(type="text", text="No coding memory matched this query.")]
    return [TextContent(type="text", text="\n".join(lines))]


async def _remember_coding_memory(engine: Engine, args: dict) -> list[TextContent]:
    backend = _memory_backend()
    if backend is None:
        return [TextContent(type="text", text="hy-memory backend unavailable.")]
    messages = args.get("messages") or []
    if not messages:
        return [TextContent(type="text", text="No messages to remember.")]
    workspace_id = args.get("workspace_id") or backend.workspace_id
    branch = args.get("branch") or backend.branch
    scoped = CodingMemory(
        user_id=backend.user_id,
        workspace_id=workspace_id,
        branch=branch,
        client=backend.client,
    )
    result = scoped.remember(messages)
    if not result.get("success"):
        return [TextContent(type="text", text=f"Coding memory write failed: {result}")]
    facts = len(result.get("fact_ids") or [])
    experiences = len(result.get("experience_ids") or [])
    return [
        TextContent(
            type="text",
            text=(
                f"Remembered (scene={result.get('scene')}): "
                f"{facts} fact(s), {experiences} experience(s)."
            ),
        )
    ]


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
