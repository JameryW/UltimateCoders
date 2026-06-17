"""Mock worker — JSON-RPC 2.0 bridge for integration testing.

Entry point: ``python -m tests.python.mock_worker``

Implements the same JSON-RPC protocol as local_worker.py but uses
simple newline-split decomposition + auto-complete subtasks without LLM.

On ``submit_task``:
  1. Split description by newlines to create subtasks
  2. Send ``task_update`` notifications sequentially with 50ms delays:
     - First notification: all subtasks Assigned, task InProgress
     - Per subtask: InProgress -> Completed
     - Final notification: all subtasks Completed, task Completed
  3. Send final submit_task response

On ``ping``: returns ``{"status": "ok"}``
On ``shutdown``: exits gracefully
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any

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


class MockWorker:
    """Mock worker that reads JSON-RPC from stdin and executes tasks with mock decomposition."""

    def __init__(self) -> None:
        self._writer = JsonRpcWriter()
        self._running = False
        # Delay between state transitions (configurable via UC_MOCK_DELAY_MS)
        self._delay_ms = int(os.environ.get("UC_MOCK_DELAY_MS", "50"))

    async def start(self) -> None:
        """Read stdin for JSON-RPC messages."""
        self._running = True
        logger.info("MockWorker ready, reading stdin for JSON-RPC messages")

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while self._running:
            try:
                line = await reader.readline()
                if not line:
                    logger.info("stdin closed, shutting down")
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                await self._handle_message(text)
            except Exception:
                logger.error("Error reading stdin", exc_info=True)
                break

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
            await self._handle_submit(id_, params)
        elif method == "shutdown":
            self._writer.write_response(id_, {"status": "ok"})
            self._running = False
        else:
            self._writer.write_error(id_, -32601, f"Method not found: {method}")

    async def _handle_submit(
        self, id_: int | str | None, params: dict[str, Any]
    ) -> None:
        """Submit a task with mock decomposition and auto-completion."""
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
        delay = self._delay_ms / 1000.0
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

    def stop(self) -> None:
        self._running = False


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    worker = MockWorker()
    try:
        await worker.start()
    except Exception:
        logger.error("MockWorker failed", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
