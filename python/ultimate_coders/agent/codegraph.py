"""CodegraphClient — direct SQLite client for codegraph knowledge graph.

Reads .codegraph/codegraph.db directly via the sqlite3 module.
Provides sub-millisecond symbol search, call graph traversal,
impact analysis, and structured context exploration.

Gracefully degrades when the database is unavailable: all methods
return empty results instead of raising errors.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

# Maximum character length for explore() Markdown output
_EXPLORE_MAX_CHARS = 2000


class CodegraphClient:
    """Direct SQLite client for codegraph knowledge graph.

    Usage:
        client = CodegraphClient("/path/to/project")
        if client.is_available():
            results = client.search("execute_subtask")
            callers = client.callers("execute_subtask")
            context = client.explore("how does subtask execution work?")
    """

    def __init__(
        self,
        project_path: str,
        engine: Any | None = None,
        repo_id: str = "",
    ) -> None:
        """Initialize with project path. Auto-detect .codegraph/codegraph.db.

        Args:
            project_path: Absolute or relative path to the project root
                containing the .codegraph/ directory. Empty string disables
                codegraph integration.
            engine: Optional gateway Engine for unified (Postgres-first)
                symbol search. When provided, ``search`` queries the gateway's
                AST index first so cross-worker symbols are visible, falling
                back to the local SQLite on miss/unavailable.
            repo_id: Repo scope for gateway queries (the subtask's project_id).
        """
        self._db_path: str = ""
        self._conn: sqlite3.Connection | None = None
        self._available: bool = False
        self._engine = engine
        self._repo_id = repo_id

        if project_path:
            self._db_path = os.path.join(project_path, ".codegraph", "codegraph.db")
            self._available = os.path.isfile(self._db_path)

    def is_available(self) -> bool:
        """Whether the codegraph database exists and is accessible."""
        return self._available

    def _get_connection(self) -> sqlite3.Connection | None:
        """Lazily open a SQLite connection.

        Returns None if the database is not available or cannot be opened.
        """
        if not self._available:
            return None
        if self._conn is not None:
            return self._conn
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for concurrent reads alongside the daemon
            conn.execute("PRAGMA journal_mode=WAL")
            self._conn = conn
            return conn
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Failed to open codegraph DB: %s", self._db_path, exc_info=True)
            self._available = False
            return None

    def close(self) -> None:
        """Close the SQLite connection if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.debug("Error closing codegraph DB connection", exc_info=True)
            self._conn = None

    def __del__(self) -> None:
        """Ensure the SQLite connection is closed on garbage collection."""
        self.close()

    # ── Core query methods ────────────────────────────────────────

    def search(
        self,
        query: str,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """FTS5 search across symbol names, qualified names, docstrings, signatures.

        When a gateway engine is configured, queries it first (AST symbol
        search across the shared Postgres-backed index) so symbols defined in
        other workers' edits are visible. Falls back to the local SQLite
        codegraph on miss or when no engine is set.

        Args:
            query: Search term (FTS5 MATCH syntax supported for local; plain
                text for gateway).
            kind: Optional node kind filter (function, method, class, struct, etc.).
            limit: Maximum number of results.

        Returns:
            List of dicts with keys: id, name, qualified_name, kind, file_path,
            language, start_line, end_line, signature, docstring.
        """
        # Gateway-first: query the shared AST index so cross-worker symbols
        # are visible. ponytail: in-process engine not wired into the stdio
        # lsp_mcp server yet — callers that construct CodegraphClient with an
        # engine get unified search for free.
        if self._engine is not None:
            try:
                from ultimate_coders.search.query import SearchQuery

                sq = (
                    SearchQuery(query)
                    .with_modes(["ast"])
                    .limit(limit)
                )
                if self._repo_id:
                    sq = sq.in_repos([self._repo_id])
                result = self._engine.search(sq)
                items = getattr(result, "items", None) or []
                out: list[dict[str, Any]] = []
                for it in items:
                    name = getattr(it, "symbol_name", None) or ""
                    if not name:
                        continue
                    if kind and getattr(it, "symbol_kind", None) != kind:
                        continue
                    out.append(
                        {
                            "id": "",
                            "name": name,
                            "qualified_name": name,
                            "kind": getattr(it, "symbol_kind", None) or "symbol",
                            "file_path": getattr(it, "file_path", ""),
                            "language": "",
                            "start_line": getattr(it, "start_line", 0),
                            "end_line": getattr(it, "end_line", 0),
                            "signature": "",
                            "docstring": getattr(it, "content_snippet", ""),
                        }
                    )
                if out:
                    return out[:limit]
            except Exception:
                logger.debug(
                    "Gateway AST search failed, falling back to local codegraph: %s",
                    query,
                    exc_info=True,
                )

        conn = self._get_connection()
        if conn is None:
            return []

        try:
            # Sanitize FTS5 query: remove special chars that break MATCH
            safe_query = _sanitize_fts5(query)
            if not safe_query:
                return []

            sql = """
                SELECT n.id, n.name, n.qualified_name, n.kind, n.file_path,
                       n.language, n.start_line, n.end_line, n.signature, n.docstring
                FROM nodes n
                JOIN nodes_fts fts ON n.id = fts.id
                WHERE nodes_fts MATCH ?
            """
            params: list[Any] = [safe_query]

            if kind:
                sql += " AND n.kind = ?"
                params.append(kind)

            sql += " ORDER BY n.start_line LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Codegraph search query failed: %s", query, exc_info=True)
            return []

    def callers(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find all callers of a symbol (who calls this function/method).

        Args:
            symbol: Symbol name to find callers for.
            limit: Maximum number of results.

        Returns:
            List of dicts with keys: name, kind, file_path, start_line.
        """
        conn = self._get_connection()
        if conn is None:
            return []

        try:
            sql = """
                SELECT n_src.name, n_src.kind, n_src.file_path, n_src.start_line
                FROM edges e
                JOIN nodes n_src ON e.source = n_src.id
                JOIN nodes n_tgt ON e.target = n_tgt.id
                WHERE n_tgt.name = ? AND e.kind = 'calls'
                ORDER BY n_src.file_path, n_src.start_line
                LIMIT ?
            """
            rows = conn.execute(sql, [symbol, limit]).fetchall()
            return [dict(row) for row in rows]

        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Codegraph callers query failed: %s", symbol, exc_info=True)
            return []

    def callees(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find what a symbol calls (what does this function/method call).

        Args:
            symbol: Symbol name to find callees for.
            limit: Maximum number of results.

        Returns:
            List of dicts with keys: name, kind, file_path, start_line.
        """
        conn = self._get_connection()
        if conn is None:
            return []

        try:
            sql = """
                SELECT n_tgt.name, n_tgt.kind, n_tgt.file_path, n_tgt.start_line
                FROM edges e
                JOIN nodes n_tgt ON e.target = n_tgt.id
                JOIN nodes n_src ON e.source = n_src.id
                WHERE n_src.name = ? AND e.kind = 'calls'
                ORDER BY n_tgt.file_path, n_tgt.start_line
                LIMIT ?
            """
            rows = conn.execute(sql, [symbol, limit]).fetchall()
            return [dict(row) for row in rows]

        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Codegraph callees query failed: %s", symbol, exc_info=True)
            return []

    def impact(self, symbol: str, depth: int = 2) -> list[dict[str, Any]]:
        """BFS traversal to find all symbols affected by changing a symbol.

        Traverses reverse dependency edges (calls, references, imports,
        instantiates) up to the specified depth.

        Args:
            symbol: Symbol name to analyze impact for.
            depth: Maximum BFS traversal depth.

        Returns:
            List of dicts with keys: name, kind, file_path, start_line.
        """
        conn = self._get_connection()
        if conn is None:
            return []

        try:
            # Find the starting node by name
            target = conn.execute(
                "SELECT id FROM nodes WHERE name = ? LIMIT 1",
                [symbol],
            ).fetchone()
            if not target:
                return []

            start_id = target[0]
            visited: set[str] = {start_id}
            affected: list[dict[str, Any]] = []
            queue: list[str] = [start_id]

            for _ in range(depth):
                next_queue: list[str] = []
                for node_id in queue:
                    # Find nodes that depend on this node
                    dependents = conn.execute(
                        """
                        SELECT DISTINCT n.id, n.name, n.kind, n.file_path, n.start_line
                        FROM edges e
                        JOIN nodes n ON e.source = n.id
                        WHERE e.target = ?
                          AND e.kind IN ('calls', 'references', 'imports', 'instantiates')
                        """,
                        [node_id],
                    ).fetchall()

                    for d in dependents:
                        dep_id = d[0]
                        if dep_id not in visited:
                            visited.add(dep_id)
                            affected.append(
                                {
                                    "name": d[1],
                                    "kind": d[2],
                                    "file_path": d[3],
                                    "start_line": d[4],
                                }
                            )
                            next_queue.append(dep_id)

                queue = next_queue

            return affected

        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Codegraph impact query failed: %s", symbol, exc_info=True)
            return []

    def explore(self, query: str, max_nodes: int = 15) -> str:
        """Build structured context for a natural language query.

        Combines search + callers/callees/impact into a Markdown summary
        suitable for injecting into an LLM prompt. Output is truncated
        to approximately 2000 characters.

        Args:
            query: Natural language query or symbol name.
            max_nodes: Maximum number of symbols to include in the summary.

        Returns:
            Markdown string with sections: Symbols, Dependencies, Impact.
            Empty string if codegraph is unavailable or no results found.
        """
        if not self.is_available():
            return ""

        # 1. Search for relevant symbols
        symbols = self.search(query, limit=max_nodes)
        if not symbols:
            return ""

        parts: list[str] = []

        # Symbols section
        parts.append("## Relevant Symbols")
        for sym in symbols[:10]:
            line_info = f"{sym['file_path']}:{sym['start_line']}"
            sig = sym.get("signature") or ""
            sig_display = f" `{sig}`" if sig else ""
            parts.append(f"- **{sym['name']}** ({sym['kind']}) @ {line_info}{sig_display}")

        # Dependencies section (for top 3 results)
        parts.append("")
        parts.append("## Dependencies")
        for sym in symbols[:3]:
            name = sym["name"]
            # Callers (1 level)
            sym_callers = self.callers(name, limit=5)
            if sym_callers:
                caller_strs = [
                    f"{c['name']} ({c['file_path']}:{c['start_line']})" for c in sym_callers[:5]
                ]
                parts.append(f"- **{name}** called by: {', '.join(caller_strs)}")

            # Callees (1 level)
            sym_callees = self.callees(name, limit=5)
            if sym_callees:
                callee_strs = [
                    f"{c['name']} ({c['file_path']}:{c['start_line']})" for c in sym_callees[:5]
                ]
                parts.append(f"- **{name}** calls: {', '.join(callee_strs)}")

        # Impact section (for top result only)
        if symbols:
            top_name = symbols[0]["name"]
            impact_results = self.impact(top_name, depth=1)
            if impact_results:
                parts.append("")
                parts.append("## Impact Analysis")
                parts.append(
                    f"Changing **{top_name}** would affect {len(impact_results)} symbol(s):"
                )
                for imp in impact_results[:10]:
                    parts.append(
                        f"- {imp['name']} ({imp['kind']}) @ {imp['file_path']}:{imp['start_line']}"
                    )

                # Affected test files
                test_files = sorted(
                    {
                        imp["file_path"]
                        for imp in impact_results
                        if any(
                            seg in imp["file_path"].lower()
                            for seg in ("test_", "_test.", "/tests/", "/test/")
                        )
                    }
                )
                if test_files:
                    parts.append("")
                    parts.append("## Affected Test Files")
                    for tf in test_files[:10]:
                        parts.append(f"- {tf}")

        result = "\n".join(parts)

        # Truncate to max chars
        if len(result) > _EXPLORE_MAX_CHARS:
            result = result[: _EXPLORE_MAX_CHARS - 20] + "\n... (truncated)"

        return result


def _sanitize_fts5(query: str) -> str:
    """Sanitize a query string for FTS5 MATCH.

    FTS5 has special characters that can cause syntax errors.
    This function strips problematic characters and ensures the query
    is safe for MATCH expressions.

    Args:
        query: Raw query string.

    Returns:
        Sanitized query string safe for FTS5 MATCH.
    """
    # Remove FTS5 special operators and characters
    # Keep alphanumeric, underscores, and dots (for qualified names)
    cleaned = re.sub(r"[^a-zA-Z0-9_.\s]", " ", query)
    # Split into tokens and rejoin with OR for broader matching
    tokens = cleaned.split()
    if not tokens:
        return ""
    # Use OR between tokens for broader FTS5 matching
    return " OR ".join(tokens)
