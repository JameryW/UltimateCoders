"""T7 #643 — cooperative cancel on the Python worker.

The Rust gateway emits `attempt_cancelled` / `subtask_cancelled` control
events on `uc.task.event` (C1+C2). The worker must kill the agent process
group(s) for exactly the targeted nodes and cancel their execution tasks —
WITHOUT touching task-level cancellation state (the node may re-run after
an attempt re-arm, and siblings keep going). Late results stay fenced on
the graph plane (`commit_once`).
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from ultimate_coders.nats_worker import NatsWorker as _NatsWorker
from ultimate_coders.agent import sandbox as sandbox_mod
from ultimate_coders.agent.sandbox import SandboxManager, SandboxConfig, _kill_process_tree


def _make_worker() -> _NatsWorker:
    """Build a NatsWorker without running start() (no NATS/IO)."""
    return _NatsWorker(project_path="/tmp/test", mode="default")


def _event_msg(event_type: str, task_id: str, **extra) -> MagicMock:
    payload = {"type": event_type, "task_id": task_id, **extra}
    msg = MagicMock()
    msg.data = json.dumps(payload).encode()
    return msg


def _fake_worker(kills: list) -> MagicMock:
    """A fake agent Worker whose kill_node records its keys and 'finds' a
    live process for every requested node."""
    w = MagicMock()

    def _kill(task_id, node_id):
        kills.append((task_id, node_id))
        return True

    w.kill_node = _kill
    return w


# ── SandboxManager.kill_group registry ──────────────────────────────


def test_kill_group_kills_registered_proc_and_pops_key():
    """A registered live proc is killed (via the tree-kill helper) and the
    registry entry is consumed; a second kill finds nothing."""
    mgr = SandboxManager(config=SandboxConfig(project_path="/tmp"))
    proc = MagicMock()
    proc.returncode = None
    seen = []
    mgr._active_procs[("t-1", "st-a")] = proc

    monkey_mod = sandbox_mod
    orig = monkey_mod._kill_process_tree
    monkey_mod._kill_process_tree = lambda p: seen.append(p)
    try:
        assert mgr.kill_group(("t-1", "st-a")) is True
        assert mgr.kill_group(("t-1", "st-a")) is False
    finally:
        monkey_mod._kill_process_tree = orig

    assert seen == [proc]
    assert ("t-1", "st-a") not in mgr._active_procs


def test_kill_group_ignores_missing_key():
    mgr = SandboxManager(config=SandboxConfig(project_path="/tmp"))
    assert mgr.kill_group(("nope", "nope")) is False


def test_kill_process_tree_skips_settled_proc():
    """A proc that already exited must not be signalled again."""
    proc = MagicMock()
    proc.returncode = 0
    proc.kill = MagicMock()
    _kill_process_tree(proc)
    proc.kill.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
async def test_kill_process_tree_reaps_posix_group():
    """End-to-end on POSIX: spawn a shell that forks a child in its own
    session, kill the group, both die."""
    proc = await asyncio.create_subprocess_shell(
        "sleep 30 & sleep 30",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.returncode is None
    _kill_process_tree(proc)
    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode is not None


# ── Worker.kill_node delegation ─────────────────────────────────────


def test_worker_kill_node_delegates_to_sandbox_registry():
    from ultimate_coders.agent.worker import Worker

    worker = Worker.__new__(Worker)  # skip heavy __init__
    seen = []

    def _kill(key):
        seen.append(key)
        return True

    worker._sandbox_manager = MagicMock()
    worker._sandbox_manager.kill_group = _kill

    assert worker.kill_node("t-1", "st-a") is True
    assert seen == [("t-1", "st-a")]


def test_worker_kill_node_without_registry_support_is_false():
    from ultimate_coders.agent.worker import Worker

    worker = Worker.__new__(Worker)
    worker._sandbox_manager = object()  # no kill_group attr
    assert worker.kill_node("t-1", "st-a") is False


# ── NatsWorker per-node cancel ──────────────────────────────────────


async def test_cancel_node_executions_kills_and_cancels_only_target():
    """The targeted node's execution task is cancelled and its process
    killed; sibling nodes and task-level cancellation state are untouched."""
    nw = _make_worker()
    kills = []
    nw._worker = _fake_worker(kills)

    sibling = asyncio.create_task(asyncio.sleep(60))
    target = asyncio.create_task(asyncio.sleep(60))
    nw._running_node_tasks[("t-1", "st-b")] = sibling
    nw._running_node_tasks[("t-1", "st-a")] = target

    moved = nw._cancel_node_executions("t-1", ["st-a"])

    assert moved == 1
    assert kills == [("t-1", "st-a")]
    await asyncio.sleep(0.01)
    assert target.cancelled()
    assert not sibling.done()
    # Task-level cancellation state deliberately untouched (attempt re-arm
    # may legitimately re-run the node).
    assert "t-1" not in nw._cancelled_task_ids
    # Settled execution dropped from the registry.
    assert ("t-1", "st-a") not in nw._running_node_tasks
    assert ("t-1", "st-b") in nw._running_node_tasks

    sibling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sibling


async def test_cancel_node_executions_with_no_worker_is_safe():
    """Worker-only mode before components init: no crash, nothing cancelled."""
    nw = _make_worker()
    assert nw._cancel_node_executions("t-1", ["st-a"]) == 0


async def test_handle_attempt_cancelled_event_routes_to_node_cancel():
    nw = _make_worker()
    kills = []
    nw._worker = _fake_worker(kills)
    nw._orchestrator = MagicMock()

    await nw._handle_task_event(
        _event_msg("attempt_cancelled", "t-1", subtask_id="st-a")
    )

    assert kills == [("t-1", "st-a")]


async def test_handle_subtask_cancelled_event_kills_whole_closure():
    """subtask_cancelled carries the downstream closure as CSV in
    data.cancelled_nodes; every listed node gets the kill."""
    nw = _make_worker()
    kills = []
    nw._worker = _fake_worker(kills)
    nw._orchestrator = MagicMock()

    await nw._handle_task_event(
        _event_msg(
            "subtask_cancelled",
            "t-1",
            subtask_id="st-root",
            data={"cancelled_nodes": "st-root,st-mid,st-leaf", "reason": "cancelled"},
        )
    )

    assert sorted(kills) == [("t-1", "st-leaf"), ("t-1", "st-mid"), ("t-1", "st-root")]


async def test_handle_task_cancelled_event_also_kills_node_groups():
    """Task-level cancel keeps its old semantics AND now kills the agent
    process groups of every running node (T7 #643)."""
    nw = _make_worker()
    kills = []
    nw._worker = _fake_worker(kills)
    nw._orchestrator = MagicMock()
    nw._orchestrator.cancel_task = AsyncMock()

    execution = asyncio.create_task(asyncio.sleep(60))
    nw._running_node_tasks[("t-1", "st-a")] = execution

    await nw._handle_task_event(_event_msg("task_cancelled", "t-1"))

    nw._orchestrator.cancel_task.assert_awaited_once_with("t-1")
    assert ("t-1", "st-a") in kills
    assert "t-1" in nw._cancelled_task_ids
    await asyncio.sleep(0.01)
    assert execution.cancelled()


async def test_late_result_after_cancel_is_acked_not_republished():
    """A cancelled execution's _execute_and_report must ack the JS message
    and NOT publish a subtask result (the graph plane fenced the attempt;
    publishing would race the re-armed node's fresh attempt)."""
    nw = _make_worker()
    nw._worker = MagicMock()
    nw._worker.execute_subtask = AsyncMock(side_effect=asyncio.CancelledError())
    nw._worker.worker_id = "w-1"
    publisher = MagicMock()
    publisher.publish_event = AsyncMock()
    publisher.publish_update = AsyncMock()
    nw._publisher = publisher

    js_msg = MagicMock()
    acks = []

    async def _ack():
        acks.append(True)

    js_msg.ack = _ack

    from ultimate_coders.agent.types import Subtask

    subtask = Subtask(
        id="st-a",
        parent_id="t-1",
        description="d",
        expected_output="",
        file_constraints=[],
    )
    await nw._execute_and_report(subtask, js_msg=js_msg)

    assert acks == [True]
    publisher.publish_event.assert_not_called()
    publisher.publish_update.assert_not_called()
