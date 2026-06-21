"""Unit tests for CodegraphClient."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock

import pytest
from ultimate_coders.agent.codegraph import CodegraphClient, _sanitize_fts5
from ultimate_coders.agent.worker import Worker

# ── Helpers ──────────────────────────────────────────────────────


def _create_test_db(db_path: str) -> None:
    """Create a minimal codegraph SQLite database for testing.

    Creates the nodes, edges, and nodes_fts tables with test data
    simulating a small codebase.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Create tables matching codegraph schema
    conn.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            language TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            start_column INTEGER NOT NULL,
            end_column INTEGER NOT NULL,
            docstring TEXT,
            signature TEXT,
            visibility TEXT,
            is_exported INTEGER DEFAULT 0,
            is_async INTEGER DEFAULT 0,
            is_static INTEGER DEFAULT 0,
            is_abstract INTEGER DEFAULT 0,
            decorators TEXT,
            type_parameters TEXT,
            updated_at INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            metadata TEXT,
            line INTEGER,
            col INTEGER,
            provenance TEXT DEFAULT NULL,
            FOREIGN KEY (source) REFERENCES nodes(id) ON DELETE CASCADE,
            FOREIGN KEY (target) REFERENCES nodes(id) ON DELETE CASCADE
        )
    """)

    # Create FTS5 virtual table
    conn.execute("""
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            id, name, qualified_name, docstring, signature,
            content='nodes', content_rowid='rowid'
        )
    """)

    # Insert test nodes
    test_nodes = [
        (
            "function:aaa",
            "function",
            "process_data",
            "module.process_data",
            "src/process.py",
            "python",
            10,
            30,
            0,
            0,
            "Process incoming data",
            "def process_data(data: list) -> dict",
            "public",
            1,
            0,
            0,
            0,
            None,
            None,
            1000,
        ),
        (
            "function:bbb",
            "function",
            "validate_input",
            "module.validate_input",
            "src/validate.py",
            "python",
            5,
            15,
            0,
            0,
            "Validate input data",
            "def validate_input(data: Any) -> bool",
            "public",
            1,
            0,
            0,
            0,
            None,
            None,
            1000,
        ),
        (
            "method:ccc",
            "method",
            "execute",
            "Service.execute",
            "src/service.py",
            "python",
            50,
            80,
            4,
            0,
            "Execute the service",
            "async def execute(self, task: Task) -> Result",
            "public",
            1,
            1,
            0,
            0,
            None,
            None,
            1000,
        ),
        (
            "class:ddd",
            "class",
            "Service",
            "Service",
            "src/service.py",
            "python",
            1,
            100,
            0,
            0,
            "Main service class",
            None,
            "public",
            1,
            0,
            0,
            0,
            None,
            None,
            1000,
        ),
        (
            "function:eee",
            "function",
            "format_output",
            "module.format_output",
            "src/format.py",
            "python",
            20,
            35,
            0,
            0,
            "Format output data",
            "def format_output(result: dict) -> str",
            "public",
            1,
            0,
            0,
            0,
            None,
            None,
            1000,
        ),
        (
            "struct:fff",
            "struct",
            "Config",
            "Config",
            "src/config.rs",
            "rust",
            5,
            20,
            0,
            0,
            "Application configuration",
            None,
            "public",
            1,
            0,
            0,
            0,
            None,
            None,
            1000,
        ),
    ]

    for node in test_nodes:
        conn.execute(
            """
            INSERT INTO nodes (
                id, kind, name, qualified_name, file_path, language,
                start_line, end_line, start_column, end_column,
                docstring, signature, visibility, is_exported,
                is_async, is_static, is_abstract, decorators,
                type_parameters, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            node,
        )
        # Insert into FTS5
        conn.execute(
            "INSERT INTO nodes_fts "
            "(id, name, qualified_name, docstring, signature) "
            "VALUES (?, ?, ?, ?, ?)",
            (node[0], node[1 + 1], node[1 + 2], node[1 + 7], node[1 + 8]),
        )

    # Insert test edges
    test_edges = [
        ("function:aaa", "function:bbb", "calls", None, 15, 4, None),
        ("method:ccc", "function:aaa", "calls", None, 60, 8, None),
        ("method:ccc", "function:eee", "calls", None, 65, 4, None),
        ("class:ddd", "method:ccc", "contains", None, None, None, None),
        ("function:aaa", "struct:fff", "references", None, 12, 10, None),
    ]

    for edge in test_edges:
        conn.execute(
            "INSERT INTO edges "
            "(source, target, kind, metadata, line, col, provenance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            edge,
        )

    conn.commit()
    conn.close()


def _make_client_with_db() -> tuple[CodegraphClient, str]:
    """Create a CodegraphClient with a temporary test database.

    Returns:
        Tuple of (client, temp_dir_path). Caller should clean up temp_dir.
    """
    tmpdir = tempfile.mkdtemp()
    codegraph_dir = os.path.join(tmpdir, ".codegraph")
    os.makedirs(codegraph_dir, exist_ok=True)
    db_path = os.path.join(codegraph_dir, "codegraph.db")
    _create_test_db(db_path)
    client = CodegraphClient(tmpdir)
    return client, tmpdir


# ── CodegraphClient tests ────────────────────────────────────────


class TestCodegraphClient:
    """Tests for CodegraphClient."""

    def test_is_available_with_db(self):
        client, tmpdir = _make_client_with_db()
        try:
            assert client.is_available()
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_is_available_without_db(self):
        client = CodegraphClient("/nonexistent/path")
        assert not client.is_available()

    def test_search_finds_symbols(self):
        client, tmpdir = _make_client_with_db()
        try:
            results = client.search("process_data")
            assert len(results) >= 1
            assert results[0]["name"] == "process_data"
            assert results[0]["kind"] == "function"
            assert "file_path" in results[0]
            assert "start_line" in results[0]
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_search_with_kind_filter(self):
        client, tmpdir = _make_client_with_db()
        try:
            results = client.search("Service", kind="class")
            assert len(results) >= 1
            assert results[0]["name"] == "Service"
            assert results[0]["kind"] == "class"
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_search_returns_empty_when_unavailable(self):
        client = CodegraphClient("/nonexistent/path")
        results = client.search("anything")
        assert results == []

    def test_search_with_no_results(self):
        client, tmpdir = _make_client_with_db()
        try:
            results = client.search("xyznonexistent123")
            assert results == []
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_callers_finds_call_sites(self):
        client, tmpdir = _make_client_with_db()
        try:
            # process_data is called by execute
            results = client.callers("process_data")
            assert len(results) >= 1
            caller_names = [r["name"] for r in results]
            assert "execute" in caller_names
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_callers_returns_empty_when_unavailable(self):
        client = CodegraphClient("/nonexistent/path")
        results = client.callers("anything")
        assert results == []

    def test_callees_finds_called_functions(self):
        client, tmpdir = _make_client_with_db()
        try:
            # execute calls process_data and format_output
            results = client.callees("execute")
            assert len(results) >= 1
            callee_names = [r["name"] for r in results]
            assert "process_data" in callee_names
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_callees_returns_empty_when_unavailable(self):
        client = CodegraphClient("/nonexistent/path")
        results = client.callees("anything")
        assert results == []

    def test_impact_traverses_dependencies(self):
        client, tmpdir = _make_client_with_db()
        try:
            # Changing validate_input affects process_data (which calls it)
            results = client.impact("validate_input", depth=2)
            assert len(results) >= 1
            affected_names = [r["name"] for r in results]
            assert "process_data" in affected_names
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_impact_depth_one(self):
        client, tmpdir = _make_client_with_db()
        try:
            # With depth=1, only direct dependents
            results = client.impact("validate_input", depth=1)
            assert len(results) >= 1
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_impact_returns_empty_when_unavailable(self):
        client = CodegraphClient("/nonexistent/path")
        results = client.impact("anything")
        assert results == []

    def test_impact_unknown_symbol(self):
        client, tmpdir = _make_client_with_db()
        try:
            results = client.impact("nonexistent_symbol_xyz")
            assert results == []
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_explore_returns_markdown(self):
        client, tmpdir = _make_client_with_db()
        try:
            result = client.explore("process data")
            assert "## Relevant Symbols" in result
            assert "process_data" in result
            assert "## Dependencies" in result
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_explore_returns_empty_when_unavailable(self):
        client = CodegraphClient("/nonexistent/path")
        result = client.explore("anything")
        assert result == ""

    def test_explore_no_results(self):
        client, tmpdir = _make_client_with_db()
        try:
            result = client.explore("xyznonexistent123query")
            assert result == ""
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)

    def test_explore_includes_impact(self):
        client, tmpdir = _make_client_with_db()
        try:
            result = client.explore("validate_input")
            # validate_input has callers (process_data calls it)
            assert "process_data" in result
        finally:
            client.close()
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)


class TestCodegraphClientDegradation:
    """Tests for graceful degradation scenarios."""

    def test_search_on_corrupted_db(self):
        """Search should return empty list on DB query error."""
        tmpdir = tempfile.mkdtemp()
        codegraph_dir = os.path.join(tmpdir, ".codegraph")
        os.makedirs(codegraph_dir, exist_ok=True)
        # Write an invalid SQLite file
        db_path = os.path.join(codegraph_dir, "codegraph.db")
        with open(db_path, "w") as f:
            f.write("not a sqlite database")
        try:
            client = CodegraphClient(tmpdir)
            # is_available checks os.path.isfile, which is True
            # but queries should fail gracefully
            results = client.search("test")
            assert results == []
            client.close()
        finally:
            os.unlink(db_path)
            os.rmdir(codegraph_dir)
            os.rmdir(tmpdir)

    def test_callers_on_missing_table(self):
        """Callers should return empty list if the table doesn't exist."""
        tmpdir = tempfile.mkdtemp()
        codegraph_dir = os.path.join(tmpdir, ".codegraph")
        os.makedirs(codegraph_dir, exist_ok=True)
        db_path = os.path.join(codegraph_dir, "codegraph.db")
        # Create a valid SQLite DB but without codegraph tables
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        conn.close()
        try:
            client = CodegraphClient(tmpdir)
            results = client.callers("test")
            assert results == []
            client.close()
        finally:
            os.unlink(db_path)
            os.rmdir(codegraph_dir)
            os.rmdir(tmpdir)

    def test_close_is_idempotent(self):
        """Calling close() multiple times should not raise."""
        client, tmpdir = _make_client_with_db()
        try:
            client.close()
            client.close()  # Should not raise
        finally:
            os.unlink(os.path.join(tmpdir, ".codegraph", "codegraph.db"))
            os.rmdir(os.path.join(tmpdir, ".codegraph"))
            os.rmdir(tmpdir)


class TestSanitizeFts5:
    """Tests for FTS5 query sanitization."""

    def test_simple_query(self):
        result = _sanitize_fts5("process_data")
        assert "process_data" in result

    def test_special_chars_removed(self):
        result = _sanitize_fts5('test("arg") + OR - AND *')
        assert '"' not in result
        assert "(" not in result
        assert ")" not in result

    def test_multi_word_joined_with_or(self):
        result = _sanitize_fts5("process data")
        assert "OR" in result

    def test_empty_query(self):
        result = _sanitize_fts5("")
        assert result == ""

    def test_only_special_chars(self):
        result = _sanitize_fts5("!!!@@@###")
        assert result == ""


