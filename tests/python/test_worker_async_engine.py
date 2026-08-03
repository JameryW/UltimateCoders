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


async def test_final_failure_event_error_is_truncated():
    """The final-failure _publish_event payload caps the error to [:200].

    The retry path already truncated (str(e)[:200]); the final-failure branch
    sent the raw str(e), which a multi-KB traceback could push past NATS
    max_payload. Both branches must cap consistently. The SubtaskResult keeps
    the full [-2000:] stderr_tail — only the EVENT payload is capped.
    """
    from ultimate_coders.agent.types import Subtask

    events: list[tuple[str, dict]] = []

    class _FastRetryWorker(Worker):
        MAX_RETRIES = 1
        RETRY_DELAYS = [0.0]

    w = _FastRetryWorker(worker_id="w-test", engine=None)

    # sandbox.execute raises → execute_subtask hits the exception retry path,
    # exhausts MAX_RETRIES, publishes subtask_failed, returns SubtaskResult.
    async def _boom(*a, **kw):
        raise RuntimeError("E" * 500)  # 500 chars, exceeds the [:200] cap

    w._sandbox_manager = MagicMock()
    w._sandbox_manager.execute = _boom
    w._context_injector = MagicMock()
    w._context_injector.build_context.return_value = ""

    async def _capture(event_type, **kw):
        events.append((event_type, kw.get("data", {})))

    w._publish_event = _capture  # type: ignore[assignment]

    result = await w.execute_subtask(Subtask(id="s1", parent_id="t1", description="d"))
    assert result.success is False
    # final-failure event published with the CAPPED error
    failed = [e for e in events if e[0] == "subtask_failed"]
    assert failed, "expected a subtask_failed event"
    assert len(failed[-1][1]["error"]) == 200  # capped, not 500

