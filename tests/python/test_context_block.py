"""T10 #652 / D10 #647 — gateway-composed context block.

Covers:
- ``_render_gateway_context_block`` (worker-side rendering of the envelope's
  ``context_block`` — injector-compatible text, truncation marker).
- Worker preference path: a dispatch-carried block is preferred over the
  local ``_context_injector`` (the graph plane's committed outputs are the
  single source of truth; worker-only mode gains dependency context).
- Fallback path: no block → local injector, behavior identical to before.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.agent.worker import Worker, _render_gateway_context_block

# ── Renderer ────────────────────────────────────────────────────


def test_render_formats_entries_like_local_injector():
    block = {
        "entries": [
            {"node_id": "n-1", "success": True, "summary": "implemented auth"},
            {"node_id": "n-2", "success": False, "summary": "failed loudly"},
        ],
        "truncated": False,
    }
    text = _render_gateway_context_block(block)
    assert "## Context from completed subtasks (gateway-composed)" in text
    assert "### Subtask n-1 (✓)" in text
    assert "Summary: implemented auth" in text
    assert "### Subtask n-2 (✗)" in text
    assert "Summary: failed loudly" in text
    # No truncation marker when the block is complete.
    assert "truncated" not in text


def test_render_empty_entries_returns_empty_string():
    assert _render_gateway_context_block({"entries": [], "truncated": False}) == ""
    assert _render_gateway_context_block({}) == ""


def test_render_surfaces_truncation_marker():
    block = {
        "entries": [{"node_id": "n-1", "success": True, "summary": "s"}],
        "truncated": True,
    }
    assert "(context truncated by gateway)" in _render_gateway_context_block(block)


def test_render_caps_individual_summary_and_tolerates_missing_fields():
    block = {
        "entries": [
            {"node_id": "n-1", "summary": "x" * 1500},
            {"success": False},
        ],
        "truncated": False,
    }
    text = _render_gateway_context_block(block)
    assert "x" * 1000 in text
    assert "x" * 1001 not in text
    # Entry without node_id/summary renders as a bare failed header.
    assert "### Subtask  (✗)" in text


# ── Worker preference / fallback ────────────────────────────────


class _CaptureWorker(Worker):
    MAX_RETRIES = 1
    RETRY_DELAYS = [0.0]


def _make_worker(captured: list[str]) -> _CaptureWorker:
    w = _CaptureWorker(worker_id="w-test", engine=None)

    async def _fake_execute_in_sandbox(subtask, context_block, workspace_handle):
        captured.append(context_block)
        return SubtaskResult(
            subtask_id=subtask.id,
            worker_id="w-test",
            summary="done",
            success=True,
        )

    w._execute_in_sandbox = _fake_execute_in_sandbox  # type: ignore[assignment]
    w._context_injector = MagicMock()
    w._context_injector.build_context.return_value = "LOCAL"

    async def _capture_event(event_type, **kw):
        return None

    w._publish_event = _capture_event  # type: ignore[assignment]
    return w


async def test_gateway_context_block_preferred_over_local_injector():
    captured: list[str] = []
    w = _make_worker(captured)
    block = {
        "entries": [
            {"node_id": "n-1", "success": True, "summary": "committed output"},
        ],
        "truncated": False,
    }
    await w.execute_subtask(
        Subtask(id="s1", parent_id="t1", description="d", depends_on=["n-1"]),
        gateway_context_block=block,
    )
    assert "committed output" in captured[0]
    assert "gateway-composed" in captured[0]
    # Gateway block present → the local injector (Orchestrator state mirror)
    # is never consulted.
    w._context_injector.build_context.assert_not_called()


async def test_missing_gateway_block_falls_back_to_local_injector():
    captured: list[str] = []
    w = _make_worker(captured)
    await w.execute_subtask(
        Subtask(id="s1", parent_id="t1", description="d", depends_on=["n-0"]),
    )
    assert captured[0] == "LOCAL"
    w._context_injector.build_context.assert_called_once_with(["n-0"])


async def test_present_but_empty_gateway_block_is_authoritative():
    """A carried block with no entries still wins — the gateway looked at the
    committed graph state and said 'nothing to add'; the local mirror (which
    may hold stale or parallel-run state) must not override that."""
    captured: list[str] = []
    w = _make_worker(captured)
    await w.execute_subtask(
        Subtask(id="s1", parent_id="t1", description="d", depends_on=["n-1"]),
        gateway_context_block={"entries": [], "truncated": False},
    )
    assert captured[0] == ""
    w._context_injector.build_context.assert_not_called()
