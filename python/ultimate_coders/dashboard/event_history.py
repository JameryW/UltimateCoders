"""Read task history from the Gateway's durable NATS event stream."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from nats.js.errors import NotFoundError


class EventHistoryUnavailableError(Exception):
    """The Gateway is not configured with a durable event stream."""


def _event_from_message(message: Any) -> dict[str, Any] | None:
    payload = json.loads(message.data)
    if not isinstance(payload, dict) or len(payload) != 1:
        return None
    name, fields = next(iter(payload.items()))
    if not isinstance(fields, dict):
        return None
    event_type = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    task_id = fields.get("task_id") or message.subject.removeprefix("task.")
    subtask_id = fields.get("subtask_id")
    data = {key: value for key, value in fields.items() if key != "task_id"}
    return {
        "timestamp": message.metadata.timestamp.isoformat(),
        "type": event_type,
        "task_id": task_id,
        "subtask_id": subtask_id,
        "data": data,
    }


async def list_event_history(
    nats_client: Any,
    *,
    task_id: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return newest-first Gateway events, cleaning up the temporary consumer."""
    limit = max(0, min(int(limit), 500))
    offset = max(0, int(offset))
    js = nats_client.jetstream()
    subject = f"task.{task_id}" if task_id else "task.>"
    try:
        subscription = await js.pull_subscribe(subject, stream="AGENT_EVENTS")
    except NotFoundError as exc:
        raise EventHistoryUnavailableError from exc
    consumer_name = None
    events: list[dict[str, Any]] = []
    try:
        consumer_name = (await subscription.consumer_info()).name
        while True:
            try:
                messages = await subscription.fetch(batch=256, timeout=0.2)
            except asyncio.TimeoutError:
                break
            if not messages:
                break
            for message in messages:
                event = _event_from_message(message)
                if event is not None:
                    events.append(event)
    finally:
        try:
            await subscription.unsubscribe()
        finally:
            if consumer_name is not None:
                await js.delete_consumer("AGENT_EVENTS", consumer_name)

    events.reverse()
    return {
        "available": True,
        "events": events[offset : offset + limit],
        "total": len(events),
        "offset": offset,
        "limit": limit,
    }
