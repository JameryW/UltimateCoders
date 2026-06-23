"""NATS Worker -- bridges gRPC TaskService with Python Orchestrator.

Long-running process that:
1. Subscribes to ``uc.task.submit`` (published by the Rust gRPC server)
2. Calls ``Orchestrator.submit_task()`` for each submission (sandbox decomposition)
3. Publishes status updates to ``uc.task.update`` (consumed by the gRPC server)
4. Publishes real-time events to ``uc.task.event`` (consumed by the gRPC server)
5. Publishes heartbeats to ``uc.heartbeat`` every 30 seconds

Entry point::

    python -m ultimate_coders.nats_worker

Environment variables::

    UC_NATS_URL          NATS server URL (default: nats://localhost:4222)
    UC_PROJECT_PATH      Project path for sandbox (default: current directory)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import uuid
from datetime import datetime, timezone
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.aio.subscription import Subscription

from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.types import (
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)
from ultimate_coders.agent.worker import Worker
from ultimate_coders.engine import Engine

logger = logging.getLogger(__name__)

# ── NATS subject constants (must match Rust server.rs) ──────────

NATS_SUBJECT_TASK_SUBMIT: str = "uc.task.submit"
NATS_SUBJECT_TASK_UPDATE: str = "uc.task.update"
NATS_SUBJECT_TASK_EVENT: str = "uc.task.event"
NATS_SUBJECT_HEARTBEAT: str = "uc.heartbeat"
NATS_SUBJECT_SUBTASK_EXECUTE: str = "uc.subtask.execute"

# ── Payload types ───────────────────────────────────────────────


def _make_task_update_payload(task: Task) -> dict[str, Any]:
    """Build a ``uc.task.update`` payload from a Task object.

    The format matches the Rust ``NatsTaskUpdate`` struct so the
    gRPC server can parse it with ``serde_json::from_slice``.
    Includes a ``message_id`` for deduplication (at-least-once NATS delivery).
    """
    import time

    ts_ms = int(time.time() * 1000)
    subtasks = []
    for st in task.subtasks:
        entry: dict[str, Any] = {
            "subtask_id": st.id,
            "status": _subtask_status_to_nats(st.status),
            "description": st.description,
            "depends_on": st.depends_on,
        }
        if st.assigned_worker is not None:
            entry["assigned_worker"] = st.assigned_worker
        if st.result is not None:
            entry["result"] = st.result.summary
        subtasks.append(entry)

    payload: dict[str, Any] = {
        "message_id": f"{task.id}:update:{ts_ms}",
        "task_id": task.id,
        "status": _task_status_to_nats(task.status),
        "subtasks": subtasks,
    }
    if task.result is not None:
        payload["result"] = task.result
    return payload


def _make_task_event_payload(
    event_type: str,
    task_id: str,
    subtask_id: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``uc.task.event`` payload.

    The format matches the Rust ``NatsTaskEvent`` struct.
    Includes a ``message_id`` for deduplication (at-least-once NATS delivery).
    """
    import time

    ts_ms = int(time.time() * 1000)
    message_id = f"{task_id}:{event_type}:{subtask_id}:{ts_ms}"
    payload: dict[str, Any] = {
        "message_id": message_id,
        "type": event_type,
        "task_id": task_id,
    }
    if subtask_id:
        payload["subtask_id"] = subtask_id
    if data:
        payload["data"] = data
    else:
        payload["data"] = {}
    return payload


def _task_status_to_nats(status: TaskStatus) -> str:
    """Convert TaskStatus enum to the string format expected by the
    Rust gRPC server's ``task_status_from_str()`` function."""
    # The Rust side expects CamelCase status names:
    # Created, Planning, InProgress, Completed, Failed, Paused
    mapping = {
        TaskStatus.CREATED: "Created",
        TaskStatus.PLANNING: "Planning",
        TaskStatus.IN_PROGRESS: "InProgress",
        TaskStatus.COMPLETED: "Completed",
        TaskStatus.FAILED: "Failed",
        TaskStatus.PAUSED: "Paused",
    }
    return mapping.get(status, status.value)


def _subtask_status_to_nats(status: SubtaskStatus) -> str:
    """Convert SubtaskStatus enum to the string format expected by
    the Rust gRPC server's ``subtask_status_from_str()`` function."""
    mapping = {
        SubtaskStatus.PENDING: "Pending",
        SubtaskStatus.ASSIGNED: "Assigned",
        SubtaskStatus.IN_PROGRESS: "InProgress",
        SubtaskStatus.COMPLETED: "Completed",
        SubtaskStatus.FAILED: "Failed",
        SubtaskStatus.CONFLICTED: "Conflicted",
    }
    return mapping.get(status, status.value)


# ── NatsPublisher ───────────────────────────────────────────────


class NatsPublisher:
    """Publishes Orchestrator state changes to NATS.

    Injected into the Orchestrator as an optional dependency so that
    every state transition is mirrored to the gRPC server's TaskStore
    via the NATS message bus.
    """

    def __init__(self, nc: NatsClient) -> None:
        self._nc = nc

    async def publish_update(self, task: Task) -> None:
        """Publish a task status update to ``uc.task.update``."""
        payload = _make_task_update_payload(task)
        await self._publish(NATS_SUBJECT_TASK_UPDATE, payload)

    async def publish_event(
        self,
        event_type: str,
        task_id: str,
        subtask_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Publish a task event to ``uc.task.event``."""
        payload = _make_task_event_payload(event_type, task_id, subtask_id, data)
        await self._publish(NATS_SUBJECT_TASK_EVENT, payload)

    async def publish_submit(
        self,
        task_id: str,
        description: str,
        project_id: str = "",
    ) -> None:
        """Publish a task submission to ``uc.task.submit``.

        Used by the Dashboard (and gRPC server) to route task submissions
        through NATS so the independent Python consumer processes them
        with the real Orchestrator (LLM decomposition + Worker execution).

        Args:
            task_id: UUID for the new task (caller generates it).
            description: Task description text.
            project_id: Optional project/repository context.
        """
        payload: dict[str, Any] = {
            "task_id": task_id,
            "description": description,
            "project_id": project_id,
        }
        await self._publish(NATS_SUBJECT_TASK_SUBMIT, payload)

    async def publish_heartbeat(self, consumer_id: str, worker_info: dict[str, Any] | None = None) -> None:
        """Publish a heartbeat to ``uc.heartbeat``."""
        payload: dict[str, Any] = {
            "consumer_id": consumer_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if worker_info:
            payload.update(worker_info)
        await self._publish(NATS_SUBJECT_HEARTBEAT, payload)

    async def _publish(self, subject: str, payload: dict[str, Any]) -> None:
        """Serialize and publish a JSON payload to a NATS subject."""
        try:
            data = json.dumps(payload).encode("utf-8")
            await self._nc.publish(subject, data)
        except Exception:
            logger.warning("Failed to publish to %s", subject, exc_info=True)


# ── NatsWorker ──────────────────────────────────────────────────


class NatsWorker:
    """Long-running NATS consumer that bridges gRPC TaskService with
    the Python Orchestrator.

    Lifecycle::

        worker = NatsWorker()
        await worker.start()   # connects, subscribes, starts heartbeat
        ...                    # runs until stop() or SIGINT
        await worker.stop()    # graceful shutdown
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        project_path: str = "",
        mode: str = "default",
    ) -> None:
        self._nats_url = nats_url
        self._project_path = project_path
        self._consumer_id = str(uuid.uuid4())
        self._mode = mode  # "default" or "worker"

        # Will be initialized in start()
        self._nc: NatsClient | None = None
        self._publisher: NatsPublisher | None = None
        self._engine: Engine | None = None
        self._orchestrator: Orchestrator | None = None
        self._worker: Worker | None = None
        self._subscriptions: list[Subscription] = []
        self._heartbeat_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._snapshot_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._running = False
        # Event-driven dispatch: set when a subtask completes/fails, wakes _execute_subtasks
        self._dispatch_event: asyncio.Event = asyncio.Event()
        # Remote worker discovery via heartbeat
        self._known_remote_workers: dict[str, dict[str, Any]] = {}
        self._cleanup_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Track current task ID for remote result collection
        self._current_task_id: str = ""

    async def start(self) -> None:
        """Connect to NATS, initialize components, and subscribe.

        Mode "default": subscribe to uc.task.submit + uc.task.event + uc.dashboard.>
        Mode "worker": subscribe to uc.subtask.execute (queue group) only
        """
        logger.info(
            "Starting NatsWorker (consumer_id=%s, nats_url=%s, mode=%s)",
            self._consumer_id,
            self._nats_url,
            self._mode,
        )

        # Connect to NATS with retry
        self._nc = await self._connect_with_retry()

        # Initialize publisher
        self._publisher = NatsPublisher(self._nc)

        # Initialize Engine, Orchestrator, Worker
        await self._init_components()

        if self._mode == "worker":
            # Worker mode: subscribe to subtask execution via queue group
            sub = await self._nc.subscribe(
                NATS_SUBJECT_SUBTASK_EXECUTE,
                queue="workers",
                cb=self._handle_subtask_execute,
            )
            self._subscriptions.append(sub)
            logger.info(
                "Subscribed to %s (queue group: workers)",
                NATS_SUBJECT_SUBTASK_EXECUTE,
            )
        else:
            # Default mode: full Orchestrator consumer
            # Subscribe to uc.task.submit
            sub = await self._nc.subscribe(
                NATS_SUBJECT_TASK_SUBMIT,
                cb=self._handle_submit,
            )
            self._subscriptions.append(sub)
            logger.info("Subscribed to %s", NATS_SUBJECT_TASK_SUBMIT)

            # Subscribe to uc.heartbeat for remote worker discovery
            hb_sub = await self._nc.subscribe(
                NATS_SUBJECT_HEARTBEAT,
                cb=self._handle_heartbeat,
            )
            self._subscriptions.append(hb_sub)
            logger.info("Subscribed to %s (remote worker discovery)", NATS_SUBJECT_HEARTBEAT)

            # Subscribe to Dashboard passthrough RPCs (uc.dashboard.>)
            dash_sub = await self._nc.subscribe(
                "uc.dashboard.>",
                cb=self._handle_dashboard_request,
            )
            self._subscriptions.append(dash_sub)
            logger.info("Subscribed to uc.dashboard.>")

            # Subscribe to uc.task.event (pause/resume from Rust gRPC server)
            event_sub = await self._nc.subscribe(
                NATS_SUBJECT_TASK_EVENT,
                cb=self._handle_task_event,
            )
            self._subscriptions.append(event_sub)
            logger.info("Subscribed to %s", NATS_SUBJECT_TASK_EVENT)

            # Start dashboard snapshot publisher
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())

            # Start stale worker cleanup loop
            self._cleanup_task = asyncio.create_task(self._stale_worker_cleanup_loop())

        # Start heartbeat loop (both modes)
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info("NatsWorker started and ready (mode=%s)", self._mode)

    async def stop(self) -> None:
        """Gracefully shut down the worker."""
        logger.info("Stopping NatsWorker")
        self._running = False

        # Cancel heartbeat
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # Cancel snapshot publisher
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass
            self._snapshot_task = None

        # Cancel stale worker cleanup
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # Unsubscribe
        for sub in self._subscriptions:
            try:
                await sub.unsubscribe()
            except Exception:
                logger.debug("Failed to unsubscribe", exc_info=True)
        self._subscriptions.clear()

        # Drain and close NATS connection
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                logger.debug("NATS drain failed", exc_info=True)
            self._nc = None

        logger.info("NatsWorker stopped")

    # ── Component initialization ─────────────────────────────────

    async def _init_components(self) -> None:
        """Initialize Engine, Orchestrator, and Worker."""
        # Engine (local mode -- shared with Orchestrator/Worker)
        try:
            self._engine = Engine(mode="local")
            logger.info("Engine initialized (local mode)")
        except ImportError:
            logger.warning(
                "Rust extension not built, Engine unavailable. "
                "Task execution will have degraded functionality."
            )
            self._engine = None

        # Orchestrator with NATS publisher hook
        self._orchestrator = Orchestrator(
            engine=self._engine,
            nats_publisher=self._publisher,
        )

        # Worker — sandbox-only, always
        from ultimate_coders.agent.sandbox import SandboxConfig

        sandbox_config = SandboxConfig(
            backend="subprocess",
            project_path=self._project_path or os.getcwd(),
        )
        self._worker = Worker(
            engine=self._engine,
            sandbox_config=sandbox_config,
            event_emitter=self._orchestrator.event_emitter,
            nats_publisher=self._publisher,
        )

        # Register the worker with the Orchestrator (await to ensure
        # registration completes before any tasks arrive)
        worker_info = self._worker.get_info()
        await self._orchestrator.register_worker(worker_info)
        logger.info(
            "Orchestrator + Worker initialized (worker_id=%s)",
            self._worker.worker_id,
        )

    # ── NATS connection with retry ───────────────────────────────

    async def _connect_with_retry(
        self,
        max_retries: int = 5,
        retry_delay: float = 2.0,
    ) -> NatsClient:
        """Connect to NATS with exponential backoff retry."""
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                nc = await nats.connect(self._nats_url)
                logger.info(
                    "Connected to NATS at %s (attempt %d)",
                    self._nats_url,
                    attempt,
                )
                return nc
            except Exception as e:
                last_error = e
                wait = retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    "NATS connection attempt %d/%d failed: %s. "
                    "Retrying in %.1fs",
                    attempt,
                    max_retries,
                    e,
                    wait,
                )
                await asyncio.sleep(wait)

        raise ConnectionError(
            f"Failed to connect to NATS after {max_retries} attempts: "
            f"{last_error}"
        )

    # ── Message handlers ─────────────────────────────────────────

    async def _handle_submit(self, msg: nats.aio.msg.Msg) -> None:  # type: ignore[name-defined]
        """Handle a ``uc.task.submit`` message.

        Parses the JSON payload and calls ``Orchestrator.submit_task()``.
        The Orchestrator's NATS publisher hook will automatically send
        ``uc.task.update`` and ``uc.task.event`` messages as the task
        progresses.
        """
        if self._orchestrator is None:
            logger.warning("Orchestrator not initialized, ignoring submit message")
            return

        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Invalid submit message payload: %s", e)
            return

        task_id = payload.get("task_id", "")
        description = payload.get("description", "")
        project_id = payload.get("project_id", "")

        if not description:
            logger.warning(
                "Submit message has empty description (task_id=%s), ignoring",
                task_id,
            )
            return

        logger.info(
            "Received task submit: task_id=%s description=%.80s project_id=%s",
            task_id,
            description,
            project_id,
        )

        try:
            task = await self._orchestrator.submit_task(
                description,
                project_id=project_id,
                task_id=task_id or None,
            )
            logger.info(
                "Task %s submitted to Orchestrator (status=%s, subtasks=%d)",
                task.id,
                task.status.value,
                len(task.subtasks),
            )

            # Reply with decomposed task if this is a request-reply message
            if msg.reply:
                reply_data = json.dumps({
                    "task_id": task.id,
                    "status": task.status.value,
                    "subtask_count": len(task.subtasks),
                    "subtasks": [
                        {
                            "id": st.id,
                            "description": st.description,
                            "depends_on": st.depends_on,
                            "status": st.status.value,
                        }
                        for st in task.subtasks
                    ],
                }).encode()
                await msg.respond(reply_data)
                logger.info(
                    "Replied to schedule trigger with %d subtasks",
                    len(task.subtasks),
                )

            # Assign and execute subtasks in background
            asyncio.create_task(self._execute_subtasks(task))

        except Exception:
            logger.error(
                "Failed to process task submit (task_id=%s)",
                task_id,
                exc_info=True,
            )

    async def _execute_subtasks(self, task: Task) -> None:
        """Assign and execute ready subtasks for a task.

        Implements the Orchestrator-Worker execution loop with concurrency:
        - Finds ALL ready subtasks (pending, dependencies met)
        - Executes them concurrently (bounded by worker max_capacity)
        - Reports results back, then checks for newly unblocked subtasks
        - Repeats until all subtasks are done or task is complete/failed
        """
        if self._orchestrator is None or self._worker is None:
            return

        max_iterations = len(task.subtasks) * 2 + 1  # safety limit
        for _ in range(max_iterations):
            # Refresh task state -- a pause/resume NATS event may have
            # changed the status between iterations.
            updated = self._orchestrator.get_task_status(task.id)
            if updated is not None:
                task = updated
            if task.status in (TaskStatus.PAUSED, TaskStatus.COMPLETED, TaskStatus.FAILED):
                break

            # Collect ready subtask IDs (not objects -- avoid stale refs).
            # Use a seen set so select_next_subtask doesn't return
            # the same subtask twice within this iteration.
            ready_ids: list[str] = []
            seen: set[str] = set()
            while True:
                next_st = self._orchestrator.select_next_subtask(task)
                if next_st is None or next_st.id in seen:
                    break
                seen.add(next_st.id)
                ready_ids.append(next_st.id)

            if not ready_ids:
                # No more ready subtasks -- either all done or blocked
                in_progress = any(
                    st.status == SubtaskStatus.IN_PROGRESS for st in task.subtasks
                )
                if not in_progress:
                    break
                # Event-driven wait: wake immediately when a subtask completes/fails
                # ponytail: 30s safety timeout prevents deadlock
                self._dispatch_event.clear()
                try:
                    await asyncio.wait_for(
                        self._dispatch_event.wait(), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    pass  # Safety check: re-evaluate ready subtasks
                updated_task = self._orchestrator.get_task_status(task.id)
                if updated_task is not None:
                    task = updated_task
                continue

            # Execute ready subtasks — remote or local depending on worker availability
            use_remote = self._has_remote_workers()
            capacity = self._worker.max_capacity
            batch_ids = ready_ids[:capacity]

            if use_remote:
                # Remote dispatch: publish uc.subtask.execute for each ready subtask
                for sid in batch_ids:
                    st = None
                    for s in task.subtasks:
                        if s.id == sid:
                            st = s
                            break
                    if st is None:
                        continue
                    await self._dispatch_remote(st)
                # Remote workers will report results via uc.task.event
                # → _handle_task_event → _dispatch_event.set() → next iteration
            else:
                # Local execution: existing _run_one logic
                async def _run_one(subtask_id: str) -> SubtaskResult:
                    """Assign, execute, and report a single subtask locally."""
                    st = None
                    for s in task.subtasks:
                        if s.id == subtask_id:
                            st = s
                            break
                    if st is None:
                        return SubtaskResult(
                            subtask_id=subtask_id,
                            worker_id=self._worker.worker_id,
                            summary="Subtask not found",
                            success=False,
                        )
                    wid = await self._orchestrator.assign_subtask(
                        st, self._worker.worker_id,
                    )
                    if wid is None:
                        logger.warning("Failed to assign subtask %s", st.id)
                        return SubtaskResult(
                            subtask_id=st.id,
                            worker_id=self._worker.worker_id,
                            summary="Assignment failed",
                            success=False,
                        )
                    if self._publisher is not None:
                        await self._publisher.publish_update(task)
                    result = await self._worker.execute_subtask(st)
                    await self._orchestrator.handle_subtask_result(result)
                    if self._publisher is not None:
                        await self._publisher.publish_update(task)
                    return result

                # Run batch concurrently
                results = await asyncio.gather(
                    *[_run_one(sid) for sid in batch_ids],
                    return_exceptions=True,
                )

                # Log any exceptions from gather
                for i, r in enumerate(results):
                    if isinstance(r, Exception):
                        logger.error(
                            "Subtask %s raised exception: %s",
                            batch_ids[i], r, exc_info=True,
                        )

            # Refresh task state
            updated_task = self._orchestrator.get_task_status(task.id)
            if updated_task is not None:
                task = updated_task

            # If task is complete, failed, or paused, stop
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED):
                break

    async def _dispatch_remote(self, subtask: Subtask) -> None:
        """Dispatch a subtask to remote Workers via NATS.

        Publishes a ``uc.subtask.execute`` message. The NATS queue group
        ensures exactly one remote Worker picks it up. Results come back
        via ``uc.task.event`` (handled by _handle_task_event).
        """
        if self._publisher is None or self._nc is None:
            return

        # Mark subtask as assigned in local Orchestrator
        self._orchestrator.assign_subtask(subtask, "remote")

        msg = json.dumps({
            "task_id": subtask.parent_id,
            "subtask_id": subtask.id,
            "description": subtask.description,
            "depends_on": subtask.depends_on,
            "file_constraints": subtask.file_constraints,
            "expected_output": subtask.expected_output,
            "timeout_seconds": subtask.timeout_seconds or 600,
        }).encode()

        await self._nc.publish(NATS_SUBJECT_SUBTASK_EXECUTE, msg)
        logger.info(
            "Dispatched subtask %s to remote workers",
            subtask.id[:8],
        )

    # ── Heartbeat loop ───────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Publish heartbeat to ``uc.heartbeat`` every 30 seconds."""
        while self._running:
            try:
                if self._publisher is not None:
                    w_info = None
                    if self._worker is not None:
                        info = self._worker.get_info()
                        w_info = {
                            "worker_id": info.id,
                            "capabilities": info.capabilities,
                            "current_load": info.current_load,
                            "max_capacity": info.max_capacity,
                        }
                    await self._publisher.publish_heartbeat(self._consumer_id, w_info)
                    logger.debug(
                        "Heartbeat sent (consumer_id=%s)", self._consumer_id
                    )
            except Exception:
                logger.warning("Heartbeat publish failed", exc_info=True)

            await asyncio.sleep(30.0)

    # ── Worker mode: subtask execution handler ────────────────────

    async def _handle_subtask_execute(self, msg: nats.aio.msg.Msg) -> None:  # type: ignore[name-defined]
        """Handle ``uc.subtask.execute`` messages (Worker mode only).

        Consumed via NATS queue group ``workers`` so each subtask is
        processed by exactly one worker.  Executes the subtask in a
        sandbox and publishes the result via ``uc.task.update``.
        """
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            logger.warning("Failed to parse uc.subtask.execute message", exc_info=True)
            return

        task_id = data.get("task_id", "")
        subtask_id = data.get("subtask_id", "")
        description = data.get("description", "")
        timeout_seconds = data.get("timeout_seconds", 600)

        if not task_id or not subtask_id:
            logger.warning("uc.subtask.execute missing task_id or subtask_id")
            return

        logger.info(
            "Executing subtask %s (task %s): %s",
            subtask_id[:8],
            task_id[:8],
            description[:60],
        )

        if self._worker is None:
            logger.error("No worker initialized, cannot execute subtask")
            return

        # Build a Subtask object for Worker.execute_subtask
        from ultimate_coders.agent.types import SubtaskStatus

        subtask = Subtask(
            id=subtask_id,
            parent_id=task_id,
            description=description,
            status=SubtaskStatus.PENDING,
            assigned_worker=self._worker.worker_id,
            depends_on=[],
            file_constraints=data.get("file_constraints", []),
            expected_output=data.get("expected_output", ""),
            timeout_seconds=timeout_seconds,
        )

        try:
            result = await self._worker.execute_subtask(subtask)
        except Exception as e:
            logger.error(
                "Subtask %s execution failed: %s", subtask_id, e, exc_info=True,
            )
            # Report failure via uc.task.update
            if self._publisher is not None:
                await self._publisher.publish_update(
                    self._make_subtask_result_task(
                        task_id, subtask_id, "Failed", str(e)[:200],
                    ),
                )
            return

        # Publish result via uc.task.update
        if self._publisher is not None:
            status = "Completed" if result.success else "Failed"
            summary = result.summary[:200] if result.summary else ""
            await self._publisher.publish_update(
                self._make_subtask_result_task(
                    task_id, subtask_id, status, summary,
                ),
            )

        logger.info(
            "Subtask %s %s",
            subtask_id[:8],
            "completed" if result.success else "failed",
        )

    def _make_subtask_result_task(
        self, task_id: str, subtask_id: str, status: str, summary: str,
    ) -> Task:
        """Build a minimal Task object for publishing subtask result via NatsPublisher.

        NatsPublisher.publish_update() needs a Task with subtasks, so we
        construct a lightweight one with just the result subtask.
        """
        from ultimate_coders.agent.types import SubtaskResult, SubtaskStatus

        st_status = SubtaskStatus.COMPLETED if status == "Completed" else SubtaskStatus.FAILED
        st = Subtask(
            id=subtask_id,
            parent_id=task_id,
            description="",
            status=st_status,
            assigned_worker=self._worker.worker_id if self._worker else "",
            depends_on=[],
            file_constraints=[],
            expected_output="",
            result=SubtaskResult(
                subtask_id=subtask_id,
                worker_id=self._worker.worker_id if self._worker else "",
                modified_files=[],
                summary=summary,
                success=status == "Completed",
            ) if summary else None,
        )
        return Task(
            id=task_id,
            description="",
            project_id="",
            status=TaskStatus.IN_PROGRESS,
            subtasks=[st],
        )

    async def _handle_task_event(self, msg: nats.aio.msg.Msg) -> None:  # type: ignore[name-defined]
        """Handle ``uc.task.event`` messages from NATS.

        Processes task_paused/resumed events originated by the Rust gRPC server,
        and subtask_completed/failed events for event-driven dispatch wake-up.
        Uses Orchestrator._local methods to avoid a feedback loop.
        """
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            logger.warning("Failed to parse uc.task.event message", exc_info=True)
            return

        event_type = data.get("type", "")
        task_id = data.get("task_id", "")

        if not task_id:
            logger.warning("uc.task.event missing task_id, ignoring")
            return

        if self._orchestrator is None:
            logger.debug("No orchestrator, ignoring uc.task.event %s", event_type)
            return

        if event_type == "task_paused":
            self._orchestrator.pause_task_local(task_id)
        elif event_type == "task_resumed":
            self._orchestrator.resume_task_local(task_id)
        elif event_type in ("subtask_completed", "subtask_failed"):
            # For remote dispatch: feed the result back into the Orchestrator
            subtask_id = data.get("subtask_id", "")
            if subtask_id and self._current_task_id == task_id:
                self._handle_remote_subtask_result(event_type, task_id, subtask_id, data)
            # Wake _execute_subtasks loop so newly-unblocked subtasks dispatch immediately            self._dispatch_event.set()
        else:
            logger.debug("Ignoring uc.task.event type=%s", event_type)

    # ── Remote worker discovery ─────────────────────────────────────

    async def _handle_heartbeat(self, msg: nats.aio.msg.Msg) -> None:  # type: ignore[name-defined]
        """Handle ``uc.heartbeat`` messages for remote worker discovery.

        Updates known_remote_workers with the sender's info.
        Skips heartbeats from our own worker_id to avoid self-discovery.
        """
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            logger.debug("Failed to parse heartbeat message", exc_info=True)
            return

        worker_id = data.get("worker_id", "")
        if not worker_id or worker_id == (self._worker.worker_id if self._worker else ""):
            return  # Skip self

        self._known_remote_workers[worker_id] = {
            "id": worker_id,
            "capabilities": data.get("capabilities", []),
            "load": data.get("current_load", 0),
            "max_capacity": data.get("max_capacity", 3),
            "last_seen": datetime.now(timezone.utc),
        }
        logger.debug(
            "Remote worker heartbeat: %s (total remote: %d)",
            worker_id[:8],
            len(self._known_remote_workers),
        )

    def _has_remote_workers(self) -> bool:
        """Whether any remote workers are currently known."""
        return len(self._known_remote_workers) > 0

    async def _stale_worker_cleanup_loop(self) -> None:
        """Periodically remove workers with no heartbeat for >90s."""
        while self._running:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            stale_cutoff = 90  # seconds
            stale_ids = [
                wid
                for wid, info in self._known_remote_workers.items()
                if (now - info["last_seen"]).total_seconds() > stale_cutoff
            ]
            for wid in stale_ids:
                del self._known_remote_workers[wid]
                logger.info("Removed stale remote worker: %s", wid[:8])

    # ── Dashboard NATS request-reply handlers ──────────────────────

    NATS_SUBJECT_DASHBOARD_SNAPSHOT: str = "uc.dashboard.snapshot"

    async def _handle_dashboard_request(self, msg: nats.aio.msg.Msg) -> None:  # type: ignore[name-defined]
        """Handle NATS request-reply for DashboardService passthrough RPCs.

        Subject format: uc.dashboard.{RpcName} (e.g. uc.dashboard.ListWorkers)
        Responds with JSON matching the proto response field names.
        """
        if self._orchestrator is None:
            await msg.respond(json.dumps({"available": False}).encode())
            return

        subject = msg.subject
        # Extract RPC name from subject: "uc.dashboard.ListWorkers" -> "ListWorkers"
        rpc_name = subject.split(".")[-1] if "." in subject else ""
        try:
            payload = json.loads(msg.data.decode()) if msg.data else {}
        except Exception:
            payload = {}

        handler = getattr(self, f"_dash_{rpc_name.lower()}", None)
        if handler is None:
            await msg.respond(
                json.dumps(
                    {"available": False, "error": f"unknown RPC: {rpc_name}"}
                ).encode()
            )
            return

        try:
            result = await handler(payload)
            await msg.respond(json.dumps(result).encode())
        except Exception as e:
            logger.warning("Dashboard handler %s failed: %s", rpc_name, e, exc_info=True)
            await msg.respond(json.dumps({"available": False, "error": str(e)}).encode())

    async def _dash_listworkers(self, _payload: dict) -> dict:
        """Return workers list from Orchestrator."""
        orch = self._orchestrator
        if orch is None:
            return {"available": False, "workers": []}
        heartbeat_timeout = orch.config.heartbeat_timeout_seconds if orch.config else 60
        workers = []
        for wid, w in orch.workers.items():
            now = datetime.now(timezone.utc)
            age = (now - w.last_heartbeat).total_seconds()
            workers.append({
                "id": w.id,
                "capabilities": list(w.capabilities),
                "current_load": w.current_load,
                "max_capacity": w.max_capacity,
                "load_percent": (
                    round(w.current_load / w.max_capacity * 100)
                    if w.max_capacity > 0
                    else 0
                ),
                "last_heartbeat": w.last_heartbeat.isoformat(),
                "heartbeat_age_seconds": round(age, 1),
                "heartbeat_stale": age > heartbeat_timeout,
                "is_available": w.is_available,
            })
        return {
            "available": True,
            "workers": workers,
            "total": len(workers),
            "available_count": sum(1 for w in workers if w["is_available"]),
        }

    async def _dash_getschedulerstatus(self, _payload: dict) -> dict:
        """Return scheduler status from Orchestrator."""
        orch = self._orchestrator
        if orch is None or orch.scheduler is None:
            return {"available": False, "is_running": False, "jobs": [], "execution_history": []}
        sched = orch.scheduler
        night_window = None
        if sched.night_window:
            nw = sched.night_window
            night_window = {"start": nw.start, "end": nw.end, "enabled": nw.enabled}
        jobs = []
        for j in sched.jobs.values():
            jobs.append({
                "id": j.id, "name": j.name, "cron": j.cron,
                "enabled": j.enabled,
                "last_run": j.last_run.isoformat() if j.last_run else None,
                "next_run": j.next_run.isoformat() if j.next_run else None,
            })
        history = []
        for h in sched.execution_history[-50:]:
            history.append({
                "job_id": h.job_id, "job_name": h.job_name,
                "executed_at": h.executed_at.isoformat() if h.executed_at else "",
                "success": h.success, "error": h.error,
            })
        return {
            "available": True, "is_running": sched.is_running,
            "night_window": night_window, "jobs": jobs,
            "execution_history": history,
        }

    async def _dash_getcircuitbreakerstatus(self, _payload: dict) -> dict:
        """Return circuit breaker + rate limiter status (deprecated — always unavailable)."""
        return {"available": False, "circuit_breaker": {}, "rate_limiter": {}}

    async def _dash_resetcircuitbreaker(self, _payload: dict) -> dict:
        """Reset circuit breaker (deprecated — always returns unavailable)."""
        return {"success": False, "error": "Circuit breaker removed (sandbox-only mode)"}

    async def _dash_triggerschedulerjob(self, payload: dict) -> dict:
        """Trigger a scheduled job."""
        orch = self._orchestrator
        if orch is None or orch.scheduler is None:
            return {
                "success": False,
                "job_id": payload.get("job_id", ""),
                "error": "Scheduler not available",
            }
        ok = orch.scheduler.trigger_job(payload.get("job_id", ""))
        return {"success": ok, "job_id": payload.get("job_id", "")}

    async def _dash_flushpendingtasks(self, _payload: dict) -> dict:
        """Flush pending tasks."""
        orch = self._orchestrator
        if orch is None:
            return {
                "success": False,
                "pending_count": 0,
                "executed_count": 0,
                "error": "Orchestrator not available",
            }
        count = orch.pending_task_count
        executed = await orch.flush_pending_tasks()
        return {"success": True, "pending_count": count, "executed_count": len(executed)}

    async def _dash_listevents(self, payload: dict) -> dict:
        """Return event log with pagination."""
        orch = self._orchestrator
        if orch is None:
            return {"available": False, "events": [], "total": 0, "offset": 0, "limit": 0}
        # ponytail: use dashboard app's event log if available, else empty
        dash_app = getattr(orch, "_dashboard_app", None)
        if dash_app is not None and hasattr(dash_app, "_event_log"):
            events = list(dash_app._event_log)
        else:
            events = []
        task_id = payload.get("task_id")
        if task_id:
            events = [e for e in events if e.get("task_id") == task_id]
        offset = payload.get("offset", 0)
        limit = min(payload.get("limit", 100), 500)
        paginated = events[offset:offset + limit]
        return {
            "available": True, "events": paginated,
            "total": len(events), "offset": offset, "limit": limit,
        }

    # ── Dashboard snapshot publisher ───────────────────────────────

    async def _snapshot_loop(self) -> None:
        """Publish DashboardSnapshot to uc.dashboard.snapshot every 5 seconds.

        This replaces the SSE full-snapshot mechanism for gRPC-Web clients.
        """
        while self._running:
            try:
                if self._nc is not None and self._orchestrator is not None:
                    snapshot = await self._build_snapshot()
                    payload = json.dumps(snapshot).encode()
                    await self._nc.publish(
                        self.NATS_SUBJECT_DASHBOARD_SNAPSHOT, payload
                    )
            except Exception:
                logger.debug("Dashboard snapshot publish failed", exc_info=True)
            await asyncio.sleep(5.0)

    async def _build_snapshot(self) -> dict:
        """Build a full DashboardSnapshot dict from Orchestrator state."""
        orch = self._orchestrator
        if orch is None:
            return {"timestamp": datetime.now(timezone.utc).isoformat()}

        # Health
        health = {"available": False, "status": "unavailable"}
        if orch.engine is not None:
            try:
                h = orch.engine.health()
                health = {
                    "available": True, "status": h.status,
                    "version": h.version, "uptime_seconds": h.uptime_seconds,
                }
            except Exception:
                pass

        # Workers
        workers = await self._dash_listworkers({})

        # Tasks
        tasks = {"available": False, "tasks": [], "total": 0, "status_counts": {}}
        if orch.tasks:
            status_counts: dict[str, int] = {}
            task_list = []
            for tid, t in orch.tasks.items():
                sv = t.status.value if hasattr(t.status, "value") else str(t.status)
                status_counts[sv] = status_counts.get(sv, 0) + 1
                task_list.append({
                    "id": t.id, "description": t.description, "status": sv,
                    "project_id": t.project_id, "subtask_count": len(t.subtasks),
                    "created_at": 0, "updated_at": 0, "subtasks": [],
                })
            tasks = {
                "available": True, "tasks": task_list,
                "total": len(task_list), "status_counts": status_counts,
            }

        # Scheduler
        scheduler = await self._dash_getschedulerstatus({})

        # Circuit breaker
        circuit_breaker = await self._dash_getcircuitbreakerstatus({})

        # Recent events
        dash_app = getattr(orch, "_dashboard_app", None)
        recent = []
        if dash_app is not None and hasattr(dash_app, "_event_log"):
            recent = list(dash_app._event_log)[:20]

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "health": health,
            "workers": workers,
            "tasks": tasks,
            "scheduler": scheduler,
            "circuit_breaker": circuit_breaker,
            "recent_events": recent,
        }


# ── Main entry point ────────────────────────────────────────────


async def main() -> None:
    """Entry point for ``python -m ultimate_coders.nats_worker``.

    Supports ``--mode worker`` to start in distributed Worker mode
    (subscribes to ``uc.subtask.execute`` via NATS queue group).
    Default mode is the full Orchestrator consumer.
    """
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="UltimateCoders NATS Worker")
    parser.add_argument(
        "--mode",
        choices=["default", "worker"],
        default="default",
        help="Run mode: 'default' (Orchestrator consumer) or 'worker' (subtask executor)",
    )
    args = parser.parse_args()

    nats_url = os.environ.get("UC_NATS_URL", "nats://localhost:4222")
    project_path = os.environ.get("UC_PROJECT_PATH", os.getcwd())

    worker = NatsWorker(
        nats_url=nats_url,
        project_path=project_path,
        mode=args.mode,
    )

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows does not support add_signal_handler
            pass

    try:
        await worker.start()
        # Wait until shutdown signal
        await stop_event.wait()
    except Exception:
        logger.error("NatsWorker failed to start", exc_info=True)
        raise
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
