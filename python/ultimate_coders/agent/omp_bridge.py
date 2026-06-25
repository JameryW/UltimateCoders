"""
OmpBridge — JSONL RPC client for the UC Orchestrator subprocess.

Spawns `uc-rpc-server.ts` via bun, sends JSONL commands on stdin,
reads JSONL responses and events from stdout.

Protocol:
  Command:  {"method": "<name>", "params": {...}, "id": <int>}
  Response: {"id": <int>, "result": {...}} | {"id": <int>, "error": "<msg>"}
  Event:    {"event": "<type>", "data": {...}}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from .types import Task, TaskStatus, SubtaskStatus


class OmpBridgeError(Exception):
    """Error from the UC RPC server."""


class OmpBridge:
    """Thin JSONL RPC client to the UC Orchestrator subprocess."""

    def __init__(self, cwd: str | None = None) -> None:
        self._cwd = cwd or os.getcwd()
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._event_handlers: list[asyncio.Event] = []  # ponytail: simple event signaling

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Start the UC RPC server subprocess."""
        server_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "packages", "uc-orchestrator", "src", "uc-rpc-server.ts",
        )
        self._proc = await asyncio.create_subprocess_exec(
            "bun", "run", server_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )
        # Wait for ready event
        line = await self._read_line()
        if not line or line.get("event") != "ready":
            raise OmpBridgeError(f"UC RPC server did not signal ready: {line}")

        # Start background reader
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        """Stop the UC RPC server subprocess."""
        if not self._proc:
            return
        try:
            await self._send("shutdown", {})
        except Exception:
            pass
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._proc.kill()
        if self._reader_task:
            self._reader_task.cancel()
        self._proc = None

    async def __aenter__(self) -> OmpBridge:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    # ── Orchestrator Methods ───────────────────────────────────

    async def submit_task(self, description: str) -> dict[str, Any]:
        """Submit a task for orchestration."""
        return await self._send("submit_task", {"description": description})

    async def cancel_task(self, task_id: str, subtask_id: str | None = None) -> dict[str, Any]:
        """Cancel a task or specific subtask."""
        params: dict[str, Any] = {"task_id": task_id}
        if subtask_id:
            params["subtask_id"] = subtask_id
        return await self._send("cancel_task", params)

    async def pause_task(self, task_id: str) -> dict[str, Any]:
        """Pause a running task."""
        return await self._send("pause_task", {"task_id": task_id})

    async def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a paused or failed task."""
        return await self._send("resume_task", {"task_id": task_id})

    async def show_status(self, task_id: str | None = None) -> dict[str, Any]:
        """Show task status."""
        params: dict[str, Any] = {}
        if task_id:
            params["task_id"] = task_id
        return await self._send("show_status", params)

    # ── Internal ───────────────────────────────────────────────

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a command and wait for response."""
        if not self._proc or not self._proc.stdin:
            raise OmpBridgeError("UC RPC server not running")

        self._req_id += 1
        req_id = self._req_id
        cmd = {"method": method, "params": params, "id": req_id}

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[req_id] = future

        payload = json.dumps(cmd) + "\n"
        self._proc.stdin.write(payload.encode())
        await self._proc.stdin.drain()

        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise OmpBridgeError(f"Timeout waiting for response to {method}")
        return result

    async def _reader_loop(self) -> None:
        """Background task reading JSONL responses from stdout."""
        assert self._proc and self._proc.stdout
        while True:
            line = await self._read_line()
            if line is None:
                break
            # Response to a pending request
            if "id" in line and line["id"] in self._pending:
                future = self._pending.pop(line["id"])
                if "error" in line:
                    future.set_exception(OmpBridgeError(line["error"]))
                else:
                    future.set_result(line.get("result", {}))
            # Async event — log for now
            # ponytail: future — expose event stream if needed

    async def _read_line(self) -> dict[str, Any] | None:
        """Read one JSON line from stdout."""
        assert self._proc and self._proc.stdout
        raw = await self._proc.stdout.readline()
        if not raw:
            return None
        try:
            return json.loads(raw.decode().strip())
        except json.JSONDecodeError:
            return None


# ── Self-check ──────────────────────────────────────────────────

if __name__ == "__main__":
    async def demo():
        async with OmpBridge() as bridge:
            r = await bridge.submit_task("Test task from OmpBridge")
            print(f"submit_task result: {r}")
            s = await bridge.show_status()
            print(f"show_status result: {s}")
    asyncio.run(demo())
