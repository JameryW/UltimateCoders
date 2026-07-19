"""F59 — Worker engine calls must use the async variants in gRPC mode.

The sync engine API is a blocking RPC (up to 30s); calling it from the
async execution flow froze the whole event loop — stalling the NATS
heartbeat toward the gateway's 90s stale-worker eviction. Engines without
*_async variants (in-memory, legacy mocks) keep the sync fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.agent.worker import Worker


def _make_worker(engine: object) -> Worker:
    return Worker(worker_id="w-test", engine=engine)


async def test_build_search_context_prefers_async_variant():
    engine = MagicMock()
    engine.search_async = AsyncMock(return_value=None)
    engine.list_repos_async = AsyncMock(return_value=[])
    w = _make_worker(engine)
    await w._build_search_context(Subtask(id="s1", description="implement auth"))
    engine.search_async.assert_awaited()
    engine.search.assert_not_called()


async def test_build_search_context_falls_back_to_sync_engine():
    # spec restricts attributes: no *_async variants → sync fallback.
    engine = MagicMock(spec=["search", "list_repos"])
    engine.search = MagicMock(return_value=None)
    engine.list_repos = MagicMock(return_value=[])
    w = _make_worker(engine)
    await w._build_search_context(Subtask(id="s1", description="implement auth"))
    engine.search.assert_called_once()


async def test_checkpoint_save_load_use_async_variants():
    engine = MagicMock()
    engine.write_memory_async = AsyncMock(return_value=None)
    engine.read_memory_async = AsyncMock(return_value='{"summary": "done"}')
    w = _make_worker(engine)
    result = SubtaskResult(
        subtask_id="s1", worker_id="w-test", summary="done", success=True,
    )
    await w._save_checkpoint("s1", result)
    engine.write_memory_async.assert_awaited_once()
    loaded = await w._load_checkpoint("s1")
    assert loaded == {"summary": "done"}
    engine.read_memory_async.assert_awaited_once()
