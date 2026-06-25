"""Orchestrator — thin bridge to the omp UCOrchestrator.

Delegates all orchestration logic (decompose, DAG, wave execution,
retry, circuit breaker, conflict detection) to the omp subprocess
via JSONL RPC. Python only handles:
1. NATS message consumption → OmpBridge call
2. Task state publishing back to NATS
3. Event emission for dashboard/TUI tracking
4. Real-time task state sync from omp for TUI rendering
"""

from __future__ import annotations

import logging
from typing import Any

from ultimate_coders.agent.omp_bridge import OmpBridge, OmpBridgeError
from ultimate_coders.agent.types import (
    OrchestratorConfig,
    Subtask,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkerInfo,
)

logger = logging.getLogger(__name__)

# ponytail: methods called by nats_worker.py that are now no-ops (omp handles them)
_NOOP_ASYNC = (
    "decompose_task assign_subtask handle_subtask_result "
    "schedule_ready_subtasks"
).split()

_NOOP_SYNC = (
    "select_next_subtask _rebuild_subtask_index _select_worker "
    "pause_task_local resume_task_local"
).split()

# Map omp subtask status → Python SubtaskStatus
_OMP_ST_TO_PY = {
    "pending": SubtaskStatus.PENDING,
    "running": SubtaskStatus.IN_PROGRESS,
    "reviewing": SubtaskStatus.IN_PROGRESS,
    "completed": SubtaskStatus.COMPLETED,
    "failed": SubtaskStatus.FAILED,
    "cancelled": SubtaskStatus.FAILED,  # ponytail: omp cancelled → Python failed (no CANCELLED in enum)
}

# Map omp task status → Python TaskStatus
_OMP_TASK_ST_TO_PY = {
    "planning": TaskStatus.PLANNING,
    "in_progress": TaskStatus.IN_PROGRESS,
    "completed": TaskStatus.COMPLETED,
    "failed": TaskStatus.FAILED,
    "cancelled": TaskStatus.FAILED,  # ponytail: no CANCELLED in TaskStatus, map to FAILED
}


class Orchestrator:
    """Thin bridge to the omp UCOrchestrator.

    All orchestration logic is delegated to the omp subprocess.
    This class provides NATS-facing task lifecycle methods and
    event emission for the dashboard/TUI.

    Task state is synced from omp via get_task() so TUI can read
    orchestrator.tasks[task_id].subtasks with live subtask status.
    """

    def __init__(
        self,
        engine: Any = None,
        config: OrchestratorConfig | None = None,
        nats_publisher: Any | None = None,
        **_kwargs: Any,  # absorb old kwargs (conflict_detector, scheduler, sandbox, etc.)
    ) -> None:
        self.engine = engine
        self.config = config or OrchestratorConfig()
        self.nats_publisher = nats_publisher
        self.tasks: dict[str, Task] = {}
        self.workers: dict[str, WorkerInfo] = {}
        self.conflict_detector = None  # omp has its own
        self.scheduler = None  # omp handles scheduling
        self.circuit_breaker = None  # omp has its own
        self.rate_limiter = None  # omp has its own
        self._bridge: OmpBridge | None = None
        self._night_window_active: bool = False
        self._pending_tasks: list[Task] = []
        from ultimate_coders.agent.event_emitter import TaskEventEmitter
        self.event_emitter = TaskEventEmitter()

    # ── Dynamic no-op dispatch ─────────────────────────────────
    # nats_worker.py calls methods that omp now handles internally.

    def __getattr__(self, name: str) -> Any:
        if name in _NOOP_ASYNC:
            async def _noop(*a: Any, **kw: Any) -> Any:
                return [] if name == "schedule_ready_subtasks" else None
            return _noop
        if name in _NOOP_SYNC:
            def _noop(*a: Any, **kw: Any) -> Any:
                return None
            return _noop
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    # ── Bridge Lifecycle ───────────────────────────────────────

    async def _ensure_bridge(self) -> OmpBridge:
        if self._bridge is None:
            self._bridge = OmpBridge()
            # Wire omp events → Python event_emitter + task state sync
            self._bridge.on_event(self._on_omp_event)
            await self._bridge.start()
        return self._bridge

    async def close(self) -> None:
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

    # ── omp Event → Python Event Bridge ───────────────────────

    def _on_omp_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Forward omp events to Python event_emitter for TUI/Dashboard."""
        task_id = data.get("task_id", "")
        subtask_id = data.get("subtask_id", "")

        # Emit to Python event_emitter (drives TUI real-time updates)
        self.event_emitter.emit(event_type, task_id=task_id, subtask_id=subtask_id, data=data)

        # Sync task state from omp on significant events
        if event_type in ("task_decomposed", "subtask_started",
                          "subtask_completed", "subtask_failed",
                          "task_completed", "wave_started"):
            # Schedule async state sync (don't block event callback)
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._sync_task_from_omp(task_id))
            except RuntimeError:
                pass  # no event loop — skip sync

    async def _sync_task_from_omp(self, task_id: str) -> None:
        """Pull live task state from omp and update local cache."""
        if not self._bridge or not task_id:
            return
        try:
            omp_task = await self._bridge.get_task(task_id)
            if omp_task:
                self._merge_omp_task(omp_task)
        except OmpBridgeError:
            logger.debug("Failed to sync task %s from omp", task_id)

    def _merge_omp_task(self, omp_task: dict[str, Any]) -> None:
        """Merge omp task state into local Python Task object."""
        task_id = omp_task.get("id", "")
        if not task_id:
            return

        # Get or create local Task
        task = self.tasks.get(task_id)
        if task is None:
            task = Task(
                id=task_id,
                description=omp_task.get("description", ""),
                status=_OMP_TASK_ST_TO_PY.get(omp_task.get("status", ""), TaskStatus.IN_PROGRESS),
            )
            self.tasks[task_id] = task

        # Update status
        task.status = _OMP_TASK_ST_TO_PY.get(omp_task.get("status", ""), task.status)

        # Sync subtasks
        omp_subtasks = omp_task.get("subtasks", [])
        if omp_subtasks:
            task.subtasks = [
                Subtask(
                    id=st.get("id", ""),
                    description=st.get("description", ""),
                    status=_OMP_ST_TO_PY.get(st.get("status", ""), SubtaskStatus.PENDING),
                    depends_on=st.get("depends_on", []),
                    result=st.get("result"),
                    error=st.get("error"),
                )
                for st in omp_subtasks
            ]

    # ── Task Lifecycle ─────────────────────────────────────────

    async def submit_task(
        self,
        description: str,
        project_id: str = "",
        _scheduled: bool = False,
        task_id: str | None = None,
    ) -> Task:
        """Submit a task for orchestration via omp."""
        if self._night_window_active and not _scheduled:
            task = Task(id=task_id or "", description=description,
                        project_id=project_id, status=TaskStatus.PAUSED)
            self._pending_tasks.append(task)
            self.tasks[task.id] = task
            return task

        bridge = await self._ensure_bridge()
        try:
            result = await bridge.submit_task(description)
            # Sync full task state from omp (gets task_id + subtasks)
            omp_task = await bridge.list_tasks()
            if omp_task:
                self._merge_omp_task(omp_task[-1])  # latest task
        except OmpBridgeError:
            logger.error("OmpBridge submit_task failed", exc_info=True)

        task = Task(id=task_id or "", description=description,
                    project_id=project_id, status=TaskStatus.IN_PROGRESS)
        self.tasks[task.id] = task
        await self._publish_event("task_submitted", task_id=task.id,
                                  data={"description": description})
        if self.nats_publisher is not None:
            await self.nats_publisher.publish_update(task)
        task.update_timestamp()
        return task

    async def cancel_task(self, task_id: str, subtask_id: str | None = None) -> bool:
        bridge = await self._ensure_bridge()
        try:
            r = await bridge.cancel_task(task_id, subtask_id)
            ok = r.get("ok", False)
        except OmpBridgeError:
            return False
        if ok:
            await self._sync_task_from_omp(task_id)
            if self.nats_publisher and task_id in self.tasks:
                await self.nats_publisher.publish_update(self.tasks[task_id])
        return ok

    async def pause_task(self, task_id: str) -> bool:
        bridge = await self._ensure_bridge()
        try:
            r = await bridge.pause_task(task_id)
            ok = r.get("ok", False)
        except OmpBridgeError:
            return False
        if ok:
            await self._sync_task_from_omp(task_id)
            if self.nats_publisher and task_id in self.tasks:
                await self.nats_publisher.publish_update(self.tasks[task_id])
        return ok

    async def resume_task(self, task_id: str) -> bool:
        bridge = await self._ensure_bridge()
        try:
            r = await bridge.resume_task(task_id)
            ok = r.get("ok", False)
        except OmpBridgeError:
            return False
        if ok:
            await self._sync_task_from_omp(task_id)
            if self.nats_publisher and task_id in self.tasks:
                await self.nats_publisher.publish_update(self.tasks[task_id])
        return ok

    def get_task_status(self, task_id: str) -> Task | None:
        """Get task from local cache. Auto-syncs from omp if bridge is active."""
        return self.tasks.get(task_id)

    # ── Worker Registration (pass-through) ─────────────────────

    async def register_worker(self, worker_info: WorkerInfo) -> None:
        self.workers[worker_info.worker_id] = worker_info

    async def unregister_worker(self, worker_id: str) -> None:
        self.workers.pop(worker_id, None)

    def refresh_heartbeat(self, worker_id: str) -> None:
        pass  # omp manages its own workers

    def get_available_workers(self) -> list[WorkerInfo]:
        return list(self.workers.values())

    # ── Night Window ───────────────────────────────────────────

    @property
    def night_window_active(self) -> bool:
        return self._night_window_active

    @night_window_active.setter
    def night_window_active(self, active: bool) -> None:
        self._night_window_active = active

    async def flush_pending_tasks(self) -> list[Task]:
        tasks = list(self._pending_tasks)
        self._pending_tasks.clear()
        for t in tasks:
            await self.submit_task(t.description, project_id=t.project_id,
                                  _scheduled=True, task_id=t.id)
        return tasks

    def pending_task_count(self) -> int:
        return len(self._pending_tasks)

    def schedule_task(self, **kwargs: Any) -> Any:
        return None

    # ── Event Publishing ───────────────────────────────────────

    async def _publish_event(self, event_type: str, task_id: str = "", **kw: Any) -> None:
        self.event_emitter.emit(event_type, task_id=task_id, **kw)
        if self.nats_publisher:
            try:
                await self.nats_publisher.publish_event(event_type, task_id=task_id, **kw)
            except Exception:
                logger.warning("Event publish failed: %s", event_type, exc_info=True)
