"""NATS Worker — bridges gRPC TaskService with Python Orchestrator.

Long-running process that:
1. Subscribes to ``uc.task.submit`` (published by the Rust gRPC server)
2. Calls ``Orchestrator.submit_task()`` for each submission (LLM/Sandbox decomposition)
3. Publishes status updates to ``uc.task.update`` (consumed by the gRPC server)
4. Publishes real-time events to ``uc.task.event`` (consumed by the gRPC server)
5. Publishes heartbeats to ``uc.heartbeat`` every 30 seconds

Entry point::

    python -m ultimate_coders.nats_worker

Environment variables::

    UC_NATS_URL          NATS server URL (default: nats://localhost:4222)
    UC_SANDBOX_MODE      Sandbox mode: "subprocess" or "" (default: "")
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

# ── Payload types ───────────────────────────────────────────────


def _make_task_update_payload(task: Task) -> dict[str, Any]:
    """Build a ``uc.task.update`` payload from a Task object.

    The format matches the Rust ``NatsTaskUpdate`` struct so the
    gRPC server can parse it with ``serde_json::from_slice``.
    """
    subtasks = []
    for st in task.subtasks:
        entry: dict[str, Any] = {
            "subtask_id": st.id,
            "status": _subtask_status_to_nats(st.status),
        }
        if st.assigned_worker is not None:
            entry["assigned_worker"] = st.assigned_worker
        if st.result is not None:
            entry["result"] = st.result.summary
        subtasks.append(entry)

    payload: dict[str, Any] = {
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
    """
    payload: dict[str, Any] = {
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

    async def publish_heartbeat(self, consumer_id: str) -> None:
        """Publish a heartbeat to ``uc.heartbeat``."""
        payload = {
            "consumer_id": consumer_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
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
        sandbox_mode: str = "",
        project_path: str = "",
    ) -> None:
        self._nats_url = nats_url
        self._sandbox_mode = sandbox_mode
        self._project_path = project_path
        self._consumer_id = str(uuid.uuid4())

        # Will be initialized in start()
        self._nc: NatsClient | None = None
        self._publisher: NatsPublisher | None = None
        self._engine: Engine | None = None
        self._orchestrator: Orchestrator | None = None
        self._worker: Worker | None = None
        self._subscriptions: list[Subscription] = []
        self._heartbeat_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._running = False

    async def start(self) -> None:
        """Connect to NATS, initialize Engine/Orchestrator/Worker,
        subscribe to ``uc.task.submit``, and start the heartbeat loop."""
        logger.info(
            "Starting NatsWorker (consumer_id=%s, nats_url=%s)",
            self._consumer_id,
            self._nats_url,
        )

        # Connect to NATS with retry
        self._nc = await self._connect_with_retry()

        # Initialize publisher
        self._publisher = NatsPublisher(self._nc)

        # Initialize Engine, Orchestrator, Worker
        await self._init_components()

        # Subscribe to uc.task.submit
        sub = await self._nc.subscribe(
            NATS_SUBJECT_TASK_SUBMIT,
            cb=self._handle_submit,
        )
        self._subscriptions.append(sub)
        logger.info("Subscribed to %s", NATS_SUBJECT_TASK_SUBMIT)

        # Start heartbeat loop
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info("NatsWorker started and ready")

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
        # Engine (local mode — shared with Orchestrator/Worker)
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

        # Worker with NATS event forwarding
        if self._sandbox_mode == "subprocess":
            from ultimate_coders.agent.sandbox import SandboxConfig

            sandbox_config = SandboxConfig(
                backend="subprocess",
                project_path=self._project_path or os.getcwd(),
            )
            self._worker = Worker(
                engine=self._engine,
                execution_mode="sandbox",
                sandbox_config=sandbox_config,
                event_emitter=self._orchestrator.event_emitter,
            )
        else:
            # Default: no LLM client configured — Orchestrator will use
            # sandbox decomposition if available, or fail gracefully
            self._worker = Worker(
                engine=self._engine,
                execution_mode="llm",
                event_emitter=self._orchestrator.event_emitter,
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
            )
            logger.info(
                "Task %s submitted to Orchestrator (status=%s, subtasks=%d)",
                task.id,
                task.status.value,
                len(task.subtasks),
            )

            # After submit_task completes, the Orchestrator's nats_publisher
            # hook has already published the update. But we also need to
            # assign and execute subtasks if workers are available.
            await self._execute_subtasks(task)

        except Exception:
            logger.error(
                "Failed to process task submit (task_id=%s)",
                task_id,
                exc_info=True,
            )

    async def _execute_subtasks(self, task: Task) -> None:
        """Assign and execute ready subtasks for a task.

        This implements the Orchestrator-Worker execution loop:
        1. Find ready subtasks (pending, dependencies met)
        2. Assign to worker
        3. Execute
        4. Report result back to Orchestrator
        5. Repeat until all subtasks are done or task is complete
        """
        if self._orchestrator is None or self._worker is None:
            return

        # Simple sequential execution: assign and execute subtasks one at a time
        max_iterations = len(task.subtasks) * 2 + 1  # safety limit
        for _ in range(max_iterations):
            # Find the next ready subtask
            next_subtask = self._orchestrator.select_next_subtask(task)
            if next_subtask is None:
                # No more ready subtasks — either all done or blocked
                break

            # Assign subtask to worker
            worker_id = await self._orchestrator.assign_subtask(
                next_subtask, self._worker.worker_id
            )
            if worker_id is None:
                logger.warning(
                    "Failed to assign subtask %s, skipping",
                    next_subtask.id,
                )
                continue

            # Publish assignment update
            if self._publisher is not None:
                await self._publisher.publish_update(task)

            # Execute the subtask
            result = await self._worker.execute_subtask(next_subtask)

            # Report result back to Orchestrator
            await self._orchestrator.handle_subtask_result(result)

            # Publish updated task state
            if self._publisher is not None:
                await self._publisher.publish_update(task)

            # Refresh task state (Orchestrator may have updated it)
            updated_task = self._orchestrator.get_task_status(task.id)
            if updated_task is not None:
                task = updated_task

            # If task is complete or failed, stop
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break

    # ── Heartbeat loop ───────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Publish heartbeat to ``uc.heartbeat`` every 30 seconds."""
        while self._running:
            try:
                if self._publisher is not None:
                    await self._publisher.publish_heartbeat(self._consumer_id)
                    logger.debug(
                        "Heartbeat sent (consumer_id=%s)", self._consumer_id
                    )
            except Exception:
                logger.warning("Heartbeat publish failed", exc_info=True)

            await asyncio.sleep(30.0)


# ── Main entry point ────────────────────────────────────────────


async def main() -> None:
    """Entry point for ``python -m ultimate_coders.nats_worker``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    nats_url = os.environ.get("UC_NATS_URL", "nats://localhost:4222")
    sandbox_mode = os.environ.get("UC_SANDBOX_MODE", "")
    project_path = os.environ.get("UC_PROJECT_PATH", os.getcwd())

    worker = NatsWorker(
        nats_url=nats_url,
        sandbox_mode=sandbox_mode,
        project_path=project_path,
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
