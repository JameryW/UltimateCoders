"""TaskEventEmitter — ring buffer for task event history with dedup.

Provides an in-process ring buffer that Worker and Orchestrator use to
store task execution events (subtask start/complete, tool calls, etc.)
for REST API queries and initial page loads.

Real-time SSE streaming is handled entirely by NATS — the Dashboard
subscribes to ``uc.task.event`` and pushes events to the browser.
The ring buffer here is only for REST API queries (e.g., GET
/dashboard/api/events).

Dedup: events with the same (task_id, subtask_id, type) within a 5s
window are silently dropped — prevents duplicate subtask_completed
events when both Worker and Orchestrator emit for the same subtask.

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
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEDUP_WINDOW_SECONDS = 5


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
            "v": 1,
            "timestamp": self.timestamp,
            "type": self.type,
            "task_id": self.task_id,
        }
        if self.subtask_id:
            result["subtask_id"] = self.subtask_id
        if self.data:
            result["data"] = self.data
        return result


def dedup_event_key(task_id: str, subtask_id: str, event_type: str) -> str:
    """Build a semantic dedup key for an event.

    ponytail: simple composite key, upgrade to include data hash if
    same-type events with different payloads need to pass through.
    """
    return f"{task_id}:{subtask_id}:{event_type}"


class TaskEventEmitter:
    """Ring buffer for task event history with dedup.

    Stores recent events in a bounded deque for REST API queries.
    Real-time SSE streaming is handled by NATS (Dashboard subscribes
    to ``uc.task.event`` directly).

    Dedup: events with the same dedup key within DEDUP_WINDOW_SECONDS
    are silently dropped.
    """

    def __init__(self, buffer_size: int = 500) -> None:
        """Create the event emitter.

        Args:
            buffer_size: Maximum number of recent events to keep
                in the ring buffer (default: 500).
        """
        self._recent: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        # ponytail: dict[key] -> last_emit_time, prune on emit
        self._dedup_seen: dict[str, float] = {}

    async def emit(
        self,
        event_type: str,
        task_id: str = "",
        subtask_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record a task event in the ring buffer.

        Deduplicates events with the same (task_id, subtask_id, type)
        within a 5-second window.

        Args:
            event_type: Event type (e.g., subtask_started, tool_call).
            task_id: Parent task ID.
            subtask_id: Subtask ID (optional).
            data: Event-specific data payload.
        """
        # Dedup check
        key = dedup_event_key(task_id, subtask_id, event_type)
        now = time.monotonic()
        last = self._dedup_seen.get(key)
        if last is not None and (now - last) < DEDUP_WINDOW_SECONDS:
            logger.debug("Dedup dropped event %s (within %ds window)", key, DEDUP_WINDOW_SECONDS)
            return
        self._dedup_seen[key] = now

        event = TaskEvent(
            type=event_type,
            task_id=task_id,
            subtask_id=subtask_id,
            data=data or {},
        )
        self._recent.append(event.to_dict())

        # Prune stale dedup entries (lazy, on every emit)
        cutoff = now - DEDUP_WINDOW_SECONDS * 2
        stale = [k for k, v in self._dedup_seen.items() if v < cutoff]
        for k in stale:
            del self._dedup_seen[k]

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
