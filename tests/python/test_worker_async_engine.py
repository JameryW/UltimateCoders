"""F59 — Worker engine calls must use the async variants in gRPC mode.

The sync engine API is a blocking RPC (up to 30s); calling it from the
async execution flow froze the whole event loop — stalling the NATS
heartbeat toward the gateway's 90s stale-worker eviction. Engines without
*_async variants (in-memory, legacy mocks) keep the sync fallback.

Note: the fake *_async methods are REAL async functions, not AsyncMock —
inspect.iscoroutinefunction(AsyncMock()) is False on Python 3.9 (CI runs
3.9), which would route the helper to the sync fallback.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.agent.worker import Worker


def _make_worker(engine: object) -> Worker:
    return Worker(worker_id="w-test", engine=engine)


async def test_build_search_context_prefers_async_variant():
    calls: list[str] = []
    engine = MagicMock()

    async def fake_search(*args, **kwargs):
        calls.append("search_async")
        return None

    async def fake_list_repos(*args, **kwargs):
        calls.append("list_repos_async")
        return []

    engine.search_async = fake_search
    engine.list_repos_async = fake_list_repos
    w = _make_worker(engine)
    await w._build_search_context(Subtask(id="s1", description="implement auth"))
    assert "search_async" in calls
    assert "list_repos_async" in calls
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
    calls: list[str] = []
    engine = MagicMock()

    async def fake_write(*args, **kwargs):
        calls.append("write_memory_async")
        return None

    async def fake_read(*args, **kwargs):
        calls.append("read_memory_async")
        return '{"summary": "done"}'

    engine.write_memory_async = fake_write
    engine.read_memory_async = fake_read
    w = _make_worker(engine)
    result = SubtaskResult(
        subtask_id="s1", worker_id="w-test", summary="done", success=True,
    )
    await w._save_checkpoint("s1", result)
    assert calls.count("write_memory_async") == 1
    loaded = await w._load_checkpoint("s1")
    assert loaded == {"summary": "done"}
    assert calls.count("read_memory_async") == 1
