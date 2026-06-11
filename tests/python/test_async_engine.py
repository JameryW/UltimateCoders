"""Unit tests for async PyEngine methods.

Verifies that all _async variants of PyEngine methods return awaitable
coroutines that resolve to the same types as their sync counterparts.
"""

from __future__ import annotations

import asyncio

import pytest
from ultimate_coders.engine import Engine


@pytest.fixture
def engine():
    """Create a local-mode engine for testing."""
    return Engine(mode="local")


# ── health_async ────────────────────────────────────────────────

class TestHealthAsync:
    """Tests for the health_async method."""

    def test_health_async_returns_awaitable(self, engine):
        """health_async() should return a coroutine (awaitable)."""
        coro = engine.health_async()
        assert asyncio.iscoroutine(coro)
        coro.close()  # clean up unawaited coroutine

    @pytest.mark.asyncio
    async def test_health_async_returns_health_status(self, engine):
        """await health_async() should return a HealthStatus object."""
        result = await engine.health_async()
        assert hasattr(result, "status")
        assert hasattr(result, "version")
        assert hasattr(result, "uptime_seconds")
        assert hasattr(result, "components")

    @pytest.mark.asyncio
    async def test_health_async_matches_sync(self, engine):
        """health_async() should return the same data as health()."""
        sync_result = engine.health()
        async_result = await engine.health_async()
        assert sync_result.status == async_result.status
        assert sync_result.version == async_result.version


# ── search_async ────────────────────────────────────────────────

class TestSearchAsync:
    """Tests for the search_async method."""

    def test_search_async_returns_awaitable(self, engine):
        """search_async() should return a coroutine."""
        from ultimate_coders._uc_core import PySearchQuery
        query = PySearchQuery(query="test")
        coro = engine.search_async(query)
        assert asyncio.iscoroutine(coro)
        coro.close()

    @pytest.mark.asyncio
    async def test_search_async_returns_search_result(self, engine):
        """await search_async() should return a SearchResult or raise."""
        from ultimate_coders._uc_core import PySearchQuery
        query = PySearchQuery(query="test query")
        try:
            result = await engine.search_async(query)
            assert hasattr(result, "items") or hasattr(result, "__len__")
        except RuntimeError:
            # Indexing feature may be disabled in this environment
            pass


# ── write_memory_async / read_memory_async ──────────────────────

class TestMemoryAsyncRoundtrip:
    """Tests for write_memory_async + read_memory_async roundtrip."""

    @pytest.mark.asyncio
    async def test_write_and_read_global_memory(self, engine):
        """write_memory_async + read_memory_async should roundtrip."""
        await engine.write_memory_async(
            "global", "test_key", "hello world",
            content_type="text",
            source_agent="test",
            importance=0.8,
            tags=["test"],
        )
        result = await engine.read_memory_async("global", "test_key")
        assert result is not None
        assert result.content == "hello world"
        assert result.content_type == "text"
        assert result.source_agent == "test"
        assert result.importance == pytest.approx(0.8, abs=0.01)
        assert result.tags == ["test"]

    @pytest.mark.asyncio
    async def test_write_and_read_task_memory(self, engine):
        """write_memory_async + read_memory_async with task scope."""
        await engine.write_memory_async(
            "task", "decisions", "Use PostgreSQL for storage",
            task_id="t-001",
        )
        result = await engine.read_memory_async(
            "task", "decisions", task_id="t-001",
        )
        assert result is not None
        assert "PostgreSQL" in result.content

    @pytest.mark.asyncio
    async def test_write_and_read_project_memory(self, engine):
        """write_memory_async + read_memory_async with project scope."""
        await engine.write_memory_async(
            "project", "architecture", "Microservices",
            project_id="p-001",
        )
        result = await engine.read_memory_async(
            "project", "architecture", project_id="p-001",
        )
        assert result is not None
        assert result.content == "Microservices"

    @pytest.mark.asyncio
    async def test_read_memory_async_not_found(self, engine):
        """read_memory_async should return None for missing keys."""
        result = await engine.read_memory_async("global", "nonexistent_key_xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_write_code_content(self, engine):
        """write_memory_async with content_type='code'."""
        await engine.write_memory_async(
            "global", "code_snippet", 'fn main() { println!("hi"); }',
            content_type="code",
            language="rust",
        )
        result = await engine.read_memory_async("global", "code_snippet")
        assert result is not None
        assert result.content_type == "code"
        assert result.language == "rust"

    @pytest.mark.asyncio
    async def test_write_diff_content(self, engine):
        """write_memory_async with content_type='diff'."""
        await engine.write_memory_async(
            "global", "patch", "--- a/file.rs\n+++ b/file.rs",
            content_type="diff",
            file_path="src/file.rs",
        )
        result = await engine.read_memory_async("global", "patch")
        assert result is not None
        assert result.content_type == "diff"
        assert result.file_path == "src/file.rs"

    @pytest.mark.asyncio
    async def test_write_reference_content(self, engine):
        """write_memory_async with content_type='reference'."""
        await engine.write_memory_async(
            "global", "ref", "",
            content_type="reference",
            uri="https://docs.rs/tokio",
            description="Tokio docs",
        )
        result = await engine.read_memory_async("global", "ref")
        assert result is not None
        assert result.content_type == "reference"
        assert result.uri == "https://docs.rs/tokio"
        assert result.description == "Tokio docs"


# ── delete_memory_async ─────────────────────────────────────────

class TestDeleteMemoryAsync:
    """Tests for delete_memory_async."""

    @pytest.mark.asyncio
    async def test_delete_memory_async(self, engine):
        """delete_memory_async should remove the entry."""
        await engine.write_memory_async("global", "to_delete", "temporary data")
        result = await engine.read_memory_async("global", "to_delete")
        assert result is not None

        await engine.delete_memory_async("global", "to_delete")
        result = await engine.read_memory_async("global", "to_delete")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_memory_async_task_scope(self, engine):
        """delete_memory_async with task scope."""
        await engine.write_memory_async(
            "task", "temp", "data", task_id="t-del",
        )
        await engine.delete_memory_async("task", "temp", task_id="t-del")
        result = await engine.read_memory_async("task", "temp", task_id="t-del")
        assert result is None


# ── search_memory_async ─────────────────────────────────────────

class TestSearchMemoryAsync:
    """Tests for search_memory_async."""

    @pytest.mark.asyncio
    async def test_search_memory_async_returns_list(self, engine):
        """search_memory_async should return a list."""
        results = await engine.search_memory_async("test query")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_memory_async_with_scope(self, engine):
        """search_memory_async with scope_type='global'."""
        results = await engine.search_memory_async(
            "test", scope_type="global",
        )
        assert isinstance(results, list)


# ── index_repo_async / get_index_state_async / remove_index_async ─

class TestIndexAsync:
    """Tests for index_repo_async, get_index_state_async, remove_index_async."""

    @pytest.mark.asyncio
    async def test_index_repo_async(self, engine):
        """index_repo_async should return an IndexResponse."""
        result = await engine.index_repo_async(
            "test-repo", "/tmp/nonexistent",
        )
        assert hasattr(result, "repo_id")
        assert result.repo_id == "test-repo"

    @pytest.mark.asyncio
    async def test_get_index_state_async(self, engine):
        """get_index_state_async should return a RepoIndexState."""
        result = await engine.get_index_state_async("test-repo")
        assert hasattr(result, "repo_id")
        assert hasattr(result, "indexed")

    @pytest.mark.asyncio
    async def test_remove_index_async(self, engine):
        """remove_index_async should complete without error."""
        await engine.remove_index_async("test-repo")


# ── Sync/async parity ──────────────────────────────────────────

class TestSyncAsyncParity:
    """Verify async methods produce the same results as sync methods."""

    @pytest.mark.asyncio
    async def test_health_parity(self, engine):
        """Sync and async health should return equivalent results."""
        sync_result = engine.health()
        async_result = await engine.health_async()
        assert sync_result.status == async_result.status
        assert sync_result.version == async_result.version
        assert sync_result.uptime_seconds == async_result.uptime_seconds

    @pytest.mark.asyncio
    async def test_memory_write_read_parity(self, engine):
        """Sync write + async read should work (and vice versa)."""
        # Sync write
        engine.write_memory("global", "parity_test", "sync written")
        # Async read
        result = await engine.read_memory_async("global", "parity_test")
        assert result is not None
        assert result.content == "sync written"

        # Async write
        await engine.write_memory_async("global", "parity_test2", "async written")
        # Sync read
        result2 = engine.read_memory("global", "parity_test2")
        assert result2 is not None
        assert result2.content == "async written"
