"""
Local worker — JSON-RPC 2.0 bridge between Rust gRPC server and Python Orchestrator.

Entry point: ``python -m ultimate_coders.local_worker``

Communication protocol: newline-delimited JSON-RPC 2.0 over stdin/stdout.
stderr is used for logging.

Supported methods:
    - ``submit_task``: decompose + execute a task via Orchestrator
    - ``ping``: health check
    - ``shutdown``: graceful shutdown signal

Notifications (worker → server):
    - ``task_update``: task/subtask status changes during execution
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any

from ultimate_coders.agent.llm import LLMClient
from ultimate_coders.agent.orchestrator import Orchestrator, TaskStatus
from ultimate_coders.agent.worker import Worker
from ultimate_coders.engine import Engine

logger = logging.getLogger(__name__)


class JsonRpcWriter:
    """Write JSON-RPC messages to stdout (one JSON per line)."""

    def __init__(self, out: Any = sys.stdout) -> None:
        self._out = out

    def write_response(self, id: int | str, result: dict[str, Any]) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "id": id, "result": result})
        self._out.write(msg + "\n")
        self._out.flush()

    def write_notification(self, method: str, params: dict[str, Any]) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self._out.write(msg + "\n")
        self._out.flush()

    def write_error(self, id: int | str | None, code: int, message: str) -> None:
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}
        )
        self._out.write(msg + "\n")
        self._out.flush()


class LocalWorker:
    """Long-running worker that reads JSON-RPC from stdin and executes tasks.

    When ``UC_MOCK_MODE=1`` is set, the worker uses simple newline-split
    decomposition and auto-completes subtasks without LLM, instead of
    initializing the full Orchestrator/Worker stack. This is useful for
    testing without LLM or maturin dependencies.

    The ``UC_MOCK_DELAY_MS`` environment variable controls the delay
    between subtask state transitions in mock mode (default: 50ms).
    """

    def __init__(self) -> None:
        self._orchestrator: Orchestrator | None = None
        self._worker: Worker | None = None
        self._writer = JsonRpcWriter()
        self._running = False
        self._mock_mode = os.environ.get("UC_MOCK_MODE", "") == "1"
        self._mock_delay_ms = int(os.environ.get("UC_MOCK_DELAY_MS", "50"))

    async def start(self) -> None:
        """Initialize Engine, Orchestrator, Worker; then read stdin."""
        self._running = True

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()

        def _signal_handler() -> None:
            logger.info("Received shutdown signal, stopping worker")
            self._running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        if self._mock_mode:
            logger.info("LocalWorker starting in MOCK mode")
        else:
            await self._init_components()

        logger.info("LocalWorker ready, reading stdin for JSON-RPC messages")
        # ponytail: synchronous line-by-line stdin read in async context
        # — fine for single-worker sequential task execution
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while self._running:
            try:
                line = await reader.readline()
                if not line:
                    # stdin closed — parent process exited
                    logger.info("stdin closed, shutting down")
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                await self._handle_message(text)
            except Exception:
                logger.error("Error reading stdin", exc_info=True)
                break

    async def _init_components(self) -> None:
        """Initialize Engine, Orchestrator, Worker."""
        sandbox_mode = os.environ.get("UC_SANDBOX_MODE", "")
        project_path = os.environ.get("UC_PROJECT_PATH", os.getcwd())

        # ponytail: map ANTHROPIC_AUTH_TOKEN → ANTHROPIC_API_KEY so LLMClient
        # picks it up from env. Some deployments use AUTH_TOKEN instead.
        if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_AUTH_TOKEN"]

        # LLM client (auto-detect API key from env)
        llm_client: LLMClient | None = None
        try:
            llm_client = LLMClient()
            logger.info("LLMClient initialized (provider=%s, model=%s)", llm_client.provider, llm_client.model)
        except Exception:
            logger.warning("LLMClient unavailable (no API key?), task decomposition will fail", exc_info=True)

        # Engine (local mode)
        try:
            engine = Engine(mode="local")
            logger.info("Engine initialized (local mode)")
        except ImportError:
            logger.warning("Rust extension not built, Engine unavailable")
            engine = None

        # Orchestrator (no NATS publisher — we use JSON-RPC notifications instead)
        self._orchestrator = Orchestrator(engine=engine, llm_client=llm_client)

        # Worker
        if sandbox_mode == "subprocess":
            from ultimate_coders.agent.sandbox import SandboxConfig

            sandbox_config = SandboxConfig(
                backend="subprocess",
                project_path=project_path,
            )
            self._worker = Worker(
                engine=engine,
                execution_mode="sandbox",
                sandbox_config=sandbox_config,
                event_emitter=self._orchestrator.event_emitter,
                llm_client=llm_client,
            )
        else:
            self._worker = Worker(
                engine=engine,
                execution_mode="llm",
                event_emitter=self._orchestrator.event_emitter,
                llm_client=llm_client,
            )

        # Register worker with Orchestrator
        worker_info = self._worker.get_info()
        await self._orchestrator.register_worker(worker_info)
        logger.info("Orchestrator + Worker initialized (worker_id=%s)", self._worker.worker_id)

    async def _handle_message(self, raw: str) -> None:
        """Parse and dispatch a JSON-RPC message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._writer.write_error(None, -32700, "Parse error")
            return

        method = msg.get("method")
        id_ = msg.get("id")
        params = msg.get("params", {})

        if method == "ping":
            self._writer.write_response(id_, {"status": "ok"})
        elif method == "submit_task":
            if self._mock_mode:
                await self._handle_submit_mock(id_, params)
            else:
                await self._handle_submit(id_, params)
        elif method == "shutdown":
            await self._handle_shutdown(id_)
        else:
            self._writer.write_error(id_, -32601, f"Method not found: {method}")

    async def _handle_submit(
        self, id_: int | str | None, params: dict[str, Any]
    ) -> None:
        """Submit a task to Orchestrator and execute subtasks."""
        if self._orchestrator is None or self._worker is None:
            self._writer.write_error(id_, -32000, "Orchestrator not initialized")
            return

        description = params.get("description", "")
        project_id = params.get("project_id", "")
        task_id = params.get("task_id") or None  # ponytail: pass gRPC server's task_id

        if not description:
            self._writer.write_error(id_, -32602, "Empty description")
            return

        try:
            task = await self._orchestrator.submit_task(description, project_id=project_id, task_id=task_id)
        except Exception as exc:
            self._writer.write_error(id_, -32001, f"Decomposition failed: {exc}")
            return

        # Notify: task submitted + initial subtask state
        self._writer.write_notification("task_update", self._task_to_params(task))

        # Execute subtasks sequentially
        await self._execute_subtasks(task)

        # Final response
        self._writer.write_response(id_, self._task_to_params(task))

    async def _execute_subtasks(self, task: Any) -> None:
        """Assign and execute ready subtasks (same pattern as nats_worker)."""
        if self._orchestrator is None or self._worker is None:
            return

        max_iterations = len(task.subtasks) * 2 + 1
        for _ in range(max_iterations):
            next_subtask = self._orchestrator.select_next_subtask(task)
            if next_subtask is None:
                break

            worker_id = await self._orchestrator.assign_subtask(
                next_subtask, self._worker.worker_id
            )
            if worker_id is None:
                logger.warning("Failed to assign subtask %s, skipping", next_subtask.id)
                continue

            # Notify: subtask assigned
            self._writer.write_notification("task_update", self._task_to_params(task))

            result = await self._worker.execute_subtask(next_subtask)
            await self._orchestrator.handle_subtask_result(result)

            # Notify: subtask completed/failed
            self._writer.write_notification("task_update", self._task_to_params(task))

            updated_task = await self._orchestrator.get_task_status(task.id)
            if updated_task is not None:
                task = updated_task

            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break

    def _task_to_params(self, task: Any) -> dict[str, Any]:
        """Convert a Task object to JSON-RPC notification params."""
        subtasks = []
        for st in task.subtasks:
            subtasks.append({
                "id": st.id,
                "description": st.description,
                "status": st.status.value if hasattr(st.status, "value") else str(st.status),
                "assigned_worker": st.assigned_worker,
                "depends_on": st.depends_on,
            })

        return {
            "task_id": task.id,
            "description": task.description,
            "project_id": task.project_id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "subtasks": subtasks,
            "result": task.result,
        }

    def stop(self) -> None:
        self._running = False

    async def _handle_submit_mock(
        self, id_: int | str | None, params: dict[str, Any]
    ) -> None:
        """Submit a task using mock decomposition (newline-split, no LLM).

        Same protocol as _handle_submit but uses simple decomposition and
        auto-completes subtasks with configurable delays. Useful for testing
        without LLM access.
        """
        import uuid

        description = params.get("description", "")
        project_id = params.get("project_id", "")

        if not description:
            self._writer.write_error(id_, -32602, "Empty description")
            return

        # Decompose by splitting on newlines
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        lines = [line.strip() for line in description.split("\n") if line.strip()]
        if not lines:
            lines = [description]

        subtasks = []
        for i, line in enumerate(lines):
            subtasks.append({
                "id": f"{task_id}-s{i}",
                "description": line,
                "status": "Pending",
                "assigned_worker": None,
                "depends_on": [],
            })

        # Notification 1: task InProgress, all subtasks Assigned
        for st in subtasks:
            st["status"] = "Assigned"
            st["assigned_worker"] = "mock-worker"

        self._writer.write_notification("task_update", {
            "task_id": task_id,
            "description": description,
            "project_id": project_id,
            "status": "InProgress",
            "subtasks": subtasks,
            "result": None,
        })

        # Per-subtask: InProgress -> Completed with delays
        delay = self._mock_delay_ms / 1000.0
        for st in subtasks:
            # InProgress
            st["status"] = "InProgress"
            self._writer.write_notification("task_update", {
                "task_id": task_id,
                "description": description,
                "project_id": project_id,
                "status": "InProgress",
                "subtasks": subtasks,
                "result": None,
            })
            await asyncio.sleep(delay)

            # Completed
            st["status"] = "Completed"
            self._writer.write_notification("task_update", {
                "task_id": task_id,
                "description": description,
                "project_id": project_id,
                "status": "InProgress",
                "subtasks": subtasks,
                "result": None,
            })
            await asyncio.sleep(delay)

        # Final response: task Completed
        result = {
            "task_id": task_id,
            "description": description,
            "project_id": project_id,
            "status": "Completed",
            "subtasks": subtasks,
            "result": "All subtasks completed",
        }
        self._writer.write_response(id_, result)

    async def _handle_shutdown(self, id_: int | str | None) -> None:
        """Handle the shutdown JSON-RPC method.

        Sends an acknowledgment and then stops the read loop so the
        process can exit gracefully.
        """
        logger.info("Received shutdown request, stopping worker")
        self._writer.write_response(id_, {"status": "ok", "message": "shutting down"})
        self._running = False


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,  # stderr for logs, stdout for JSON-RPC
    )
    worker = LocalWorker()

    try:
        await worker.start()
    except Exception:
        logger.error("LocalWorker failed", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
