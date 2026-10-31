"""Shared stdio transport runner for the UC MCP servers.

The MCP Python SDK changed its stdio API between releases: early builds exposed
``stdio_server(server).serve()``, while current 1.x releases expose an async
context manager plus ``Server.run(read_stream, write_stream, init_options)``.
The three UC MCP servers (engine / fs / lsp) previously called the removed
form, so they could not start with any current SDK release.

This helper implements the 1.x contract — the version range pinned by the
``mcp`` extra in ``pyproject.toml`` — and fails with an actionable message on an
incompatible SDK instead of a bare ``AttributeError``.
"""

from __future__ import annotations

import asyncio
from typing import Any


def run_stdio_server(server: Any) -> None:
    """Serve ``server`` over stdio using the current MCP SDK (1.x) contract."""
    if server is None:
        raise SystemExit(
            "mcp package not installed — install the optional extra: "
            "pip install 'ultimate-coders[mcp]'"
        )
    if not hasattr(server, "run") or not hasattr(server, "create_initialization_options"):
        raise SystemExit(
            "incompatible mcp SDK: the 1.x Server API is required "
            "(Server.run + Server.create_initialization_options); "
            "pin 'mcp>=1,<2' (e.g. pip install 'mcp>=1,<2')"
        )

    try:
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "mcp package not installed — install the optional extra: "
            "pip install 'ultimate-coders[mcp]'"
        ) from exc

    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_serve())
