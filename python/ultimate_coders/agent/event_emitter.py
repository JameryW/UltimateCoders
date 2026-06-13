"""TaskEventEmitter — real-time event bus for dashboard task tracking.

Provides an in-process event bus that Worker and Orchestrator use to
emit task execution events (subtask start/complete, LLM interactions,
tool calls) which the Dashboard SSE endpoint consumes for real-time
browser updates.

Events flow:
    Worker → TaskEventEmitter.emit() → asyncio.Queue → Dashboard SSE

Usage:
    emitter = TaskEventEmitter()

    # Emit from Worker
    await emitter.emit("tool_call", task_id="t1", subtask_id="s1",
                       data={"tool": "read_file", "input": {...}})

    # Consume in Dashboard SSE
    event = await emitter.wait_for_event(timeout=5.0)
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TaskEvent:
    """A single task execution event."""

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    type: str = ""
    task_id: str = ""
    subtask_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result: dict[str, Any] = {
            "timestamp": self.timestamp,
            "type": self.type,
            "task_id": self.task_id,
        }
        if self.subtask_id:
            result["subtask_id"] = self.subtask_id
        if self.data:
            result["data"] = self.data
        return result


class TaskEventEmitter:
    """Real-time event bus for dashboard task tracking.

    Thread-safe event emitter using asyncio.Queue. Producers (Worker,
    Orchestrator) call emit(), consumers (Dashboard SSE) call
    wait_for_event().

    Also maintains a ring buffer of recent events for REST API queries
    and initial page loads.
    """

    def __init__(self, buffer_size: int = 500) -> None:
        """Create the event emitter.

        Args:
            buffer_size: Maximum number of recent events to keep
                in the ring buffer (default: 500).
        """
        self._queue: asyncio.Queue[TaskEvent | None] = asyncio.Queue()
        self._recent: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._listeners: int = 0

    async def emit(
        self,
        event_type: str,
        task_id: str = "",
        subtask_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Emit a task event to all consumers.

        Args:
            event_type: Event type (e.g., subtask_started, tool_call).
            task_id: Parent task ID.
            subtask_id: Subtask ID (optional).
            data: Event-specific data payload.
        """
        event = TaskEvent(
            type=event_type,
            task_id=task_id,
            subtask_id=subtask_id,
            data=data or {},
        )
        event_dict = event.to_dict()

        # Store in ring buffer for REST queries
        self._recent.append(event_dict)

        # Push to queue for SSE consumers
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event queue full, dropping event: %s", event_type)

    async def wait_for_event(self, timeout: float = 5.0) -> TaskEvent | None:
        """Wait for the next event, with timeout.

        Used by the Dashboard SSE endpoint to get events as they arrive.
        Returns None on timeout, which the SSE loop can use to send
        a periodic full snapshot.

        Args:
            timeout: Maximum seconds to wait (default: 5.0).

        Returns:
            The next TaskEvent, or None on timeout.
        """
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def get_recent_events(
        self,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get recent events from the ring buffer.

        Args:
            task_id: Optional filter by task ID.
            limit: Maximum events to return (default: 100).

        Returns:
            List of event dicts, newest first.
        """
        events = list(self._recent)
        if task_id:
            events = [e for e in events if e.get("task_id") == task_id]
        return events[:limit]

    @property
    def pending_count(self) -> int:
        """Number of events waiting in the queue."""
        return self._queue.qsize()
