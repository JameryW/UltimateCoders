"""TaskEventEmitter — ring buffer for task event history.

Provides an in-process ring buffer that Worker and Orchestrator use to
store task execution events (subtask start/complete, tool calls, etc.)
for REST API queries and initial page loads.

Real-time SSE streaming is handled entirely by NATS — the Dashboard
subscribes to ``uc.task.event`` and pushes events to the browser.
The ring buffer here is only for REST API queries (e.g., GET
/dashboard/api/events).

Usage:
    emitter = TaskEventEmitter()

    # Emit from Worker / Orchestrator
    await emitter.emit("tool_call", task_id="t1", subtask_id="s1",
                       data={"tool": "read_file", "input": {...}})

    # Query recent events (REST API)
    events = emitter.get_recent_events(task_id="t1")
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

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
    """Ring buffer for task event history.

    Stores recent events in a bounded deque for REST API queries.
    Real-time SSE streaming is handled by NATS (Dashboard subscribes
    to ``uc.task.event`` directly).
    """

    def __init__(self, buffer_size: int = 500) -> None:
        """Create the event emitter.

        Args:
            buffer_size: Maximum number of recent events to keep
                in the ring buffer (default: 500).
        """
        self._recent: deque[dict[str, Any]] = deque(maxlen=buffer_size)

    async def emit(
        self,
        event_type: str,
        task_id: str = "",
        subtask_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record a task event in the ring buffer.

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
        self._recent.append(event.to_dict())

    def get_recent_events(
        self,
        task_id: Optional[str] = None,  # noqa: UP045
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
