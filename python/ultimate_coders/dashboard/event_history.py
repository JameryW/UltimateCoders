"""Read task history from the Gateway's durable NATS event stream."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from nats.js.errors import NotFoundError

logger = logging.getLogger(__name__)

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,128}$")
MAX_HISTORY_MESSAGES = 5000


class EventHistoryUnavailableError(Exception):
    """The Gateway is not configured with a durable event stream."""


def _event_from_message(message: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(message.data)
    except (ValueError, TypeError, UnicodeDecodeError):
        logger.debug("Skipping malformed JetStream event message", exc_info=True)
        return None
    if not isinstance(payload, dict) or len(payload) != 1:
        return None
    name, fields = next(iter(payload.items()))
    if not isinstance(fields, dict):
        return None
    try:
        timestamp = message.metadata.timestamp.isoformat()
    except AttributeError:
        return None
    event_type = re.sub(r"(?<!^)(?=[A-Z])", "_", str(name)).lower()
    subject = str(getattr(message, "subject", "") or "")
    task_id = fields.get("task_id") or subject.removeprefix("task.")
    subtask_id = fields.get("subtask_id")
    data = {key: value for key, value in fields.items() if key != "task_id"}
    return {
        "timestamp": timestamp,
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
    cleaned_task_id = str(task_id or "").strip()
    if cleaned_task_id and not _TASK_ID_RE.fullmatch(cleaned_task_id):
        raise ValueError("invalid task_id")
    limit = max(0, min(int(limit), 500))
    offset = max(0, int(offset))
    max_messages = max(MAX_HISTORY_MESSAGES, offset + limit)
    js = nats_client.jetstream()
    subject = f"task.{cleaned_task_id}" if cleaned_task_id else "task.>"
    try:
        subscription = await js.pull_subscribe(subject, stream="AGENT_EVENTS")
    except NotFoundError as exc:
        raise EventHistoryUnavailableError from exc
    consumer_name = None
    events: list[dict[str, Any]] = []
    fetched = 0
    try:
        consumer_name = (await subscription.consumer_info()).name
        while fetched < max_messages:
            batch_size = min(256, max_messages - fetched)
            try:
                messages = await subscription.fetch(batch=batch_size, timeout=0.2)
            except asyncio.TimeoutError:
                break
            if not messages:
                break
            fetched += len(messages)
            for message in messages:
                event = _event_from_message(message)
                if event is not None:
                    events.append(event)
    finally:
        try:
            await subscription.unsubscribe()
        except Exception:
            logger.debug("Failed to unsubscribe temporary JetStream consumer", exc_info=True)
        if consumer_name is not None:
            try:
                await js.delete_consumer("AGENT_EVENTS", consumer_name)
            except Exception:
                logger.debug(
                    "Failed to delete temporary JetStream consumer %s",
                    consumer_name,
                    exc_info=True,
                )

    events.reverse()
    return {
        "available": True,
        "events": events[offset : offset + limit],
        "total": len(events),
        "offset": offset,
        "limit": limit,
    }
