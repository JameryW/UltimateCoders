"""Night-window exclusive mode wiring in Orchestrator + nats_worker.

Tests cover (acceptance criteria from PRD):
- Flag toggle (set_night_window_active True/False) + read-only property
- Defer: night_window_active=True + scheduled=False → _pending_tasks, no dispatch
- Bypass: night_window_active=True + scheduled=True → immediate dispatch
- Flush: flush_pending_tasks drains the backlog (re-submits via submit_task)
- Inactive: night_window_active=False → all tasks execute normally (backward compat)
- Payload scheduled flag: _handle_submit reads it and passes _scheduled through
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.types import TaskStatus


def _llm_response(text: str) -> MagicMock:
    """Build a mock LLMResponse with .text."""
    resp = MagicMock()
    resp.text = text
    return resp


def _subtask_json_list(items: list[dict]) -> str:
    return json.dumps(items)


def _st(desc: str) -> dict:
    return {
        "description": desc,
        "depends_on": [],
        "file_constraints": [],
        "expected_output": "",
    }


def _make_orchestrator() -> Orchestrator:
    """Build an Orchestrator with a mock LLM that returns one subtask."""
    llm = MagicMock()
    llm.complete = AsyncMock(
        return_value=_llm_response(_subtask_json_list([_st("Do the thing")]))
    )
    return Orchestrator(llm_client=llm)


# ── Flag toggle ───────────────────────────────────────────────


class TestNightWindowFlagToggle:
    """Verify set_night_window_active + night_window_active property."""

    def test_default_is_false(self):
        orch = _make_orchestrator()
        assert orch.night_window_active is False

    def test_set_true(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)
        assert orch.night_window_active is True

    def test_set_false_after_true(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)
        orch.set_night_window_active(False)
        assert orch.night_window_active is False

    def test_set_same_value_is_noop(self):
        """Setting the same value should not log/toggle (idempotent)."""
        orch = _make_orchestrator()
        orch.set_night_window_active(True)
        # Setting True again — flag stays True, no error
        orch.set_night_window_active(True)
        assert orch.night_window_active is True

    def test_pending_tasks_starts_empty(self):
        orch = _make_orchestrator()
        assert orch._pending_tasks == []


# ── Defer: active + not scheduled ─────────────────────────────


class TestNightWindowDefer:
    """When night_window_active=True + _scheduled=False → defer."""

    async def test_defers_realtime_task_when_active(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)

        task = await orch.submit_task("Fix the bug", task_id="t-defer")

        assert task.status == TaskStatus.PAUSED
        assert task.subtasks == []
        assert len(orch._pending_tasks) == 1
        assert orch._pending_tasks[0]["task_id"] == "t-defer"
        assert orch._pending_tasks[0]["description"] == "Fix the bug"
        # The deferred task is registered in tasks dict
        assert "t-defer" in orch.tasks
        # LLM decomposition was NOT called (deferred before decompose)
        orch.llm_client.complete.assert_not_called()

    async def test_defer_preserves_project_id_and_agent_config(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)
        agent_config = {"tools": ["edit"]}

        await orch.submit_task(
            "Fix the bug",
            project_id="proj-1",
            task_id="t-defer2",
            agent_config=agent_config,
        )

        entry = orch._pending_tasks[0]
        assert entry["project_id"] == "proj-1"
        assert entry["agent_config"] == agent_config

    async def test_defer_generates_task_id_when_none(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)

        task = await orch.submit_task("Fix the bug")

        assert task.id.startswith("t-")
        assert orch._pending_tasks[0]["task_id"] == task.id


# ── Bypass: active + scheduled ────────────────────────────────


class TestNightWindowBypass:
    """When night_window_active=True + _scheduled=True → immediate dispatch."""

    async def test_scheduled_bypasses_deferral(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)

        task = await orch.submit_task(
            "Nightly rebuild", task_id="t-sched", _scheduled=True
        )

        # Scheduled tasks execute normally even when window is active
        assert task.status == TaskStatus.IN_PROGRESS
        assert len(task.subtasks) == 1
        assert orch._pending_tasks == []
        # LLM decomposition WAS called (bypassed deferral)
        orch.llm_client.complete.assert_called_once()


# ── Inactive: backward compat ─────────────────────────────────


class TestNightWindowInactive:
    """When night_window_active=False → all tasks execute normally."""

    async def test_realtime_executes_when_inactive(self):
        orch = _make_orchestrator()
        # night_window_active is False by default

        task = await orch.submit_task("Fix the bug", task_id="t-normal")

        assert task.status == TaskStatus.IN_PROGRESS
        assert len(task.subtasks) == 1
        assert orch._pending_tasks == []

    async def test_scheduled_executes_when_inactive(self):
        orch = _make_orchestrator()

        task = await orch.submit_task(
            "Nightly rebuild", task_id="t-sched2", _scheduled=True
        )

        assert task.status == TaskStatus.IN_PROGRESS
        assert len(task.subtasks) == 1
        assert orch._pending_tasks == []


# ── Flush: drain the backlog ──────────────────────────────────


class TestFlushPendingTasks:
    """flush_pending_tasks drains _pending_tasks and re-submits."""

    async def test_flush_empty_is_noop(self):
        orch = _make_orchestrator()
        result = await orch.flush_pending_tasks()
        assert result == []

    async def test_flush_drains_backlog(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)

        # Defer two tasks
        await orch.submit_task("Task A", task_id="t-a")
        await orch.submit_task("Task B", task_id="t-b")
        assert len(orch._pending_tasks) == 2

        # Close the window (so flush doesn't re-defer)
        orch.set_night_window_active(False)

        executed = await orch.flush_pending_tasks()

        assert len(executed) == 2
        assert orch._pending_tasks == []
        # Both tasks should now be IN_PROGRESS with subtasks (re-submitted)
        for task in executed:
            assert task.status == TaskStatus.IN_PROGRESS
            assert len(task.subtasks) == 1

    async def test_flush_re_defers_if_window_reopens(self):
        """If the window reopens mid-flush, deferred tasks re-defer."""
        orch = _make_orchestrator()
        orch.set_night_window_active(True)
        await orch.submit_task("Task A", task_id="t-a")

        # Reopen the window before flushing — flush will re-defer
        # (set_night_window_active is already True; flush calls submit_task
        # with _scheduled=False, which defers again)
        executed = await orch.flush_pending_tasks()

        # The task was re-deferred (not executed)
        assert len(executed) == 1
        assert executed[0].status == TaskStatus.PAUSED
        assert len(orch._pending_tasks) == 1

    async def test_flush_preserves_task_ids(self):
        orch = _make_orchestrator()
        orch.set_night_window_active(True)
        await orch.submit_task("Task A", task_id="t-original")

        orch.set_night_window_active(False)
        executed = await orch.flush_pending_tasks()

        assert len(executed) == 1
        assert executed[0].id == "t-original"


# ── Payload scheduled flag (nats_worker._handle_submit) ───────


class TestHandleSubmitScheduledFlag:
    """Verify _handle_submit reads payload `scheduled` and passes it through."""

    def _make_worker(self) -> MagicMock:
        """Build a NatsWorker-like mock with just enough to test _handle_submit."""
        worker = MagicMock()
        worker._orchestrator = _make_orchestrator()
        # _spawn_bg receives a coroutine; close it to avoid "never awaited"
        # warnings while still tracking call counts via the mock.
        def _consume_bg(coro):
            coro.close()
        worker._spawn_bg = MagicMock(side_effect=_consume_bg)
        # _execute_subtasks is a coroutine — return a dummy coro that
        # completes immediately (avoids "never awaited" warnings).
        async def _dummy_exec(task):
            pass
        worker._execute_subtasks = lambda task: _dummy_exec(task)
        return worker

    def _make_msg(self, payload: dict) -> MagicMock:
        msg = MagicMock()
        msg.data = json.dumps(payload).encode("utf-8")
        msg.reply = None
        return msg

    async def test_realtime_payload_defers_when_active(self):
        """Payload without `scheduled` (real-time) defers when window active."""
        from ultimate_coders.nats_worker import NatsWorker

        worker = self._make_worker()
        worker._orchestrator.set_night_window_active(True)

        msg = self._make_msg({
            "task_id": "t-rt",
            "description": "Real-time task",
            "project_id": "proj",
        })

        await NatsWorker._handle_submit(worker, msg)

        assert len(worker._orchestrator._pending_tasks) == 1
        # _execute_subtasks should NOT have been spawned (deferred)
        worker._spawn_bg.assert_not_called()

    async def test_scheduled_payload_bypasses_when_active(self):
        """Payload with `scheduled: true` bypasses deferral."""
        from ultimate_coders.nats_worker import NatsWorker

        worker = self._make_worker()
        worker._orchestrator.set_night_window_active(True)

        msg = self._make_msg({
            "task_id": "t-sched",
            "description": "Scheduled task",
            "project_id": "proj",
            "scheduled": True,
        })

        await NatsWorker._handle_submit(worker, msg)

        assert worker._orchestrator._pending_tasks == []
        # _execute_subtasks SHOULD have been spawned (bypassed)
        worker._spawn_bg.assert_called_once()

    async def test_realtime_payload_executes_when_inactive(self):
        """Payload without `scheduled` executes normally when window inactive."""
        from ultimate_coders.nats_worker import NatsWorker

        worker = self._make_worker()
        # night_window_active is False by default

        msg = self._make_msg({
            "task_id": "t-rt",
            "description": "Real-time task",
            "project_id": "proj",
        })

        await NatsWorker._handle_submit(worker, msg)

        assert worker._orchestrator._pending_tasks == []
        worker._spawn_bg.assert_called_once()

    async def test_scheduled_false_explicit_executes_when_inactive(self):
        """Payload with `scheduled: false` (explicit) executes normally."""
        from ultimate_coders.nats_worker import NatsWorker

        worker = self._make_worker()

        msg = self._make_msg({
            "task_id": "t-rt",
            "description": "Real-time task",
            "project_id": "proj",
            "scheduled": False,
        })

        await NatsWorker._handle_submit(worker, msg)

        assert worker._orchestrator._pending_tasks == []
        worker._spawn_bg.assert_called_once()
