"""Agent radio: lightweight worklog and mention traffic between agent nodes.

AgentRadio (#13) gives the graph one narrow side channel.  A node publishes
short *worklog* entries so the rest of the graph can see what it is doing, and
peers publish *mentions* addressed to one node.  Mentions are collected by a
:class:`BackgroundMentionWatcher` and rendered into a small, bounded prompt
block at the next safe turn boundary -- radio traffic never interrupts the
agent mid-step and never grows a prompt without a cap.

The transport is deliberately abstract.  :class:`RadioBus` is a structural
protocol; :class:`InMemoryRadioBus` is the dependency-free implementation used
by tests and single-process workers.  A NATS-backed bus can be supplied by a
deployment, but ``nats`` is **never imported at module import time**, so this
module stays importable in minimal environments.

Subject layout (one token per graph, one extra token per node)::

    uc.radio.<graph_id>.worklog
    uc.radio.<graph_id>.mention.<node_id>

Everything here is opt-in and safe to import while ``UC_AGENT_RADIO`` is unset.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_DRAIN_LIMIT",
    "DEFAULT_MAX_PROMPT_CHARS",
    "KIND_MENTION",
    "KIND_THREAD",
    "KIND_WORKLOG",
    "RADIO_ENABLED_ENV",
    "BackgroundMentionWatcher",
    "InMemoryRadioBus",
    "RadioBus",
    "RadioMessage",
    "mention_subject",
    "radio_enabled",
    "subject_for",
    "worklog_subject",
]

RADIO_ENABLED_ENV = "UC_AGENT_RADIO"

KIND_WORKLOG = "worklog"
KIND_MENTION = "mention"
KIND_THREAD = "thread"
RADIO_KINDS = frozenset({KIND_WORKLOG, KIND_MENTION, KIND_THREAD})

DEFAULT_DRAIN_LIMIT = 20
DEFAULT_MAX_PROMPT_CHARS = 4000

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_INVALID_SUBJECT_CHARS = frozenset({"*", ">", " ", "\t", "\r", "\n", "/"})


def radio_enabled() -> bool:
    """Feature flag: AgentRadio is enabled only when ``UC_AGENT_RADIO`` is truthy."""

    return os.environ.get(RADIO_ENABLED_ENV, "").strip().lower() in _TRUTHY


def _subject_token(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} must be a non-empty string")
    if any(char in _INVALID_SUBJECT_CHARS for char in text):
        raise ValueError(
            f"{label} contains characters that are invalid in a NATS subject: {text!r}"
        )
    return text


def worklog_subject(graph_id: str) -> str:
    """Return the worklog subject for ``graph_id``."""

    graph = _subject_token(graph_id, "graph_id")
    return f"uc.radio.{graph}.{KIND_WORKLOG}"


def mention_subject(graph_id: str, node_id: str) -> str:
    """Return the mention subject for ``node_id`` in ``graph_id``."""

    graph = _subject_token(graph_id, "graph_id")
    node = _subject_token(node_id, "node_id")
    return f"uc.radio.{graph}.{KIND_MENTION}.{node}"


def subject_for(message: RadioMessage) -> str:
    """Return the delivery subject for one message.

    Worklog entries go to the graph worklog subject.  Mentions and thread
    replies both address a single node, so both use that node's mention
    subject; a thread reply carries its parent id in ``metadata``.
    """

    if message.kind == KIND_WORKLOG:
        return worklog_subject(message.graph_id)
    return mention_subject(message.graph_id, message.node_id)


@dataclass(frozen=True)
class RadioMessage:
    """One immutable radio message."""

    message_id: str
    graph_id: str
    node_id: str
    kind: str
    text: str
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        message_id = str(self.message_id or "").strip()
        graph_id = str(self.graph_id or "").strip()
        node_id = str(self.node_id or "").strip()
        kind = str(self.kind or "").strip().lower()
        text = str(self.text or "").strip()
        if not message_id:
            raise ValueError("message_id must be a non-empty string")
        if not graph_id:
            raise ValueError("graph_id must be a non-empty string")
        if not node_id:
            raise ValueError("node_id must be a non-empty string")
        if kind not in RADIO_KINDS:
            raise ValueError(f"unknown radio kind: {self.kind!r}")
        if not text:
            raise ValueError("text must be a non-empty string")
        try:
            created_at = float(self.created_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("created_at must be a number") from exc
        metadata = dict(self.metadata or {})
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "graph_id", graph_id)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping of this message."""

        return {
            "message_id": self.message_id,
            "graph_id": self.graph_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "text": self.text,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RadioMessage:
        """Rebuild a message from :meth:`to_dict` output (missing fields tolerated)."""

        if not isinstance(data, Mapping):
            raise TypeError("RadioMessage.from_dict expects a mapping")
        return cls(
            message_id=data.get("message_id", ""),
            graph_id=data.get("graph_id", ""),
            node_id=data.get("node_id", ""),
            kind=data.get("kind", ""),
            text=data.get("text", ""),
            created_at=data.get("created_at", 0.0),
            metadata=data.get("metadata") or {},
        )


@runtime_checkable
class RadioBus(Protocol):
    """Structural transport protocol for agent radio."""

    def publish(self, message: RadioMessage) -> None:
        """Publish one message to its derived subject."""

    def subscribe(self, subject: str, handler: Callable[[RadioMessage], None]) -> None:
        """Register ``handler`` for messages published to ``subject``."""

    def history(self, subject: str | None = None) -> list[RadioMessage]:
        """Return retained messages, optionally filtered by exact subject."""


class InMemoryRadioBus:
    """Thread-safe in-process :class:`RadioBus` implementation.

    Messages are retained in publication order (bounded only by caller
    discipline -- this is a test/development bus, not a durable queue).
    Handlers are invoked synchronously after the message is stored; a handler
    that publishes another message is safe because handlers run outside the
    storage lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: dict[str, list[RadioMessage]] = {}
        self._order: list[RadioMessage] = []
        self._subscribers: dict[str, list[Callable[[RadioMessage], None]]] = {}

    def publish(self, message: RadioMessage) -> None:
        if not isinstance(message, RadioMessage):
            raise TypeError("publish expects a RadioMessage")
        subject = subject_for(message)
        with self._lock:
            self._messages.setdefault(subject, []).append(message)
            self._order.append(message)
            handlers = list(self._subscribers.get(subject, ()))
        for handler in handlers:
            handler(message)

    def subscribe(self, subject: str, handler: Callable[[RadioMessage], None]) -> None:
        key = str(subject or "").strip()
        if not key:
            raise ValueError("subject must be non-empty")
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            self._subscribers.setdefault(key, []).append(handler)

    def unsubscribe(self, subject: str, handler: Callable[[RadioMessage], None]) -> None:
        """Remove one previously registered handler (no error when absent)."""

        key = str(subject or "").strip()
        with self._lock:
            handlers = self._subscribers.get(key, [])
            if handler in handlers:
                handlers.remove(handler)
            if not handlers:
                self._subscribers.pop(key, None)

    def history(self, subject: str | None = None) -> list[RadioMessage]:
        with self._lock:
            if subject is None:
                return list(self._order)
            return list(self._messages.get(str(subject), ()))

    def clear(self) -> None:
        """Drop all retained messages and subscribers (test helper)."""

        with self._lock:
            self._messages.clear()
            self._order.clear()
            self._subscribers.clear()


class BackgroundMentionWatcher:
    """Collect mentions for one node and render them at prompt boundaries.

    The watcher subscribes to the node's mention subject and keeps an inbox of
    undrained messages.  ``post`` publishes worklog entries; ``create_thread``
    publishes a thread reply to a specific node.  ``drain_for_prompt`` is the
    only consumer: it marks the returned messages drained, is bounded by both
    a message count (default 20) and a character budget (default 4000), and
    leaves anything that does not fit for a later turn.
    """

    def __init__(
        self,
        bus: RadioBus,
        *,
        graph_id: str,
        node_id: str,
        max_pending: int = DEFAULT_DRAIN_LIMIT,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be >= 1")
        if max_prompt_chars < 1:
            raise ValueError("max_prompt_chars must be >= 1")
        self.bus = bus
        self.graph_id = _subject_token(graph_id, "graph_id")
        self.node_id = _subject_token(node_id, "node_id")
        self.max_pending = int(max_pending)
        self.max_prompt_chars = int(max_prompt_chars)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._inbox: list[RadioMessage] = []
        self._drained: set[str] = set()
        self._dropped = 0
        self._subject = mention_subject(self.graph_id, self.node_id)
        bus.subscribe(self._subject, self._on_message)

    # ── delivery ─────────────────────────────────────────────────

    def _on_message(self, message: RadioMessage) -> None:
        """Bus subscription callback."""

        if message.message_id in self._drained:
            return
        with self._lock:
            if any(item.message_id == message.message_id for item in self._inbox):
                return
            self._inbox.append(message)
            overflow = len(self._inbox) - self.max_pending
            if overflow > 0:
                dropped = self._inbox[:overflow]
                del self._inbox[:overflow]
                self._dropped += len(dropped)

    def pending(self) -> list[RadioMessage]:
        """Return undrained mentions in arrival order (bounded by ``max_pending``)."""

        with self._lock:
            return [item for item in self._inbox if item.message_id not in self._drained]

    @property
    def dropped_count(self) -> int:
        """Number of mentions dropped because the pending inbox was full."""

        with self._lock:
            return self._dropped

    # ── publishing ───────────────────────────────────────────────

    def post(self, text: str, *, metadata: Mapping[str, Any] | None = None) -> RadioMessage:
        """Publish a worklog entry for this node."""

        message = self._make_message(KIND_WORKLOG, text, metadata)
        self.bus.publish(message)
        return message

    def mention(
        self,
        text: str,
        *,
        target_node_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RadioMessage:
        """Publish a mention addressed to a node (this node by default)."""

        node_id = self.node_id if target_node_id is None else str(target_node_id)
        message = RadioMessage(
            message_id=self._id_factory(),
            graph_id=self.graph_id,
            node_id=node_id,
            kind=KIND_MENTION,
            text=str(text or ""),
            created_at=float(self._clock()),
            metadata=dict(metadata or {}),
        )
        self.bus.publish(message)
        return message

    def create_thread(
        self,
        text: str,
        *,
        parent_id: str = "",
        target_node_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RadioMessage:
        """Publish a thread reply, optionally to another node.

        A thread reply is delivered to the target node's mention subject (the
        watcher's own node by default) so it reaches the peer's next prompt.
        """

        node_id = self.node_id if target_node_id is None else str(target_node_id)
        merged = dict(metadata or {})
        if parent_id:
            merged["parent_id"] = str(parent_id)
        message = RadioMessage(
            message_id=self._id_factory(),
            graph_id=self.graph_id,
            node_id=node_id,
            kind=KIND_THREAD,
            text=str(text or ""),
            created_at=float(self._clock()),
            metadata=merged,
        )
        self.bus.publish(message)
        return message

    def _make_message(
        self, kind: str, text: str, metadata: Mapping[str, Any] | None
    ) -> RadioMessage:
        return RadioMessage(
            message_id=self._id_factory(),
            graph_id=self.graph_id,
            node_id=self.node_id,
            kind=kind,
            text=str(text or ""),
            created_at=float(self._clock()),
            metadata=dict(metadata or {}),
        )

    # ── prompt rendering ─────────────────────────────────────────

    def format_update(self, messages: Sequence[RadioMessage] | RadioMessage) -> str:
        """Render messages as a compact prompt block (empty input -> empty string)."""

        if isinstance(messages, RadioMessage):
            items: list[RadioMessage] = [messages]
        else:
            items = list(messages)
        if not items:
            return ""
        lines = ["Radio updates:"]
        for message in items:
            label = "thread" if message.kind == KIND_THREAD else message.kind
            parent = str(message.metadata.get("parent_id") or "").strip()
            suffix = f" (re {parent[:12]})" if parent else ""
            body = " ".join(message.text.split())
            lines.append(f"- [{label}] {message.node_id}{suffix}: {body}")
        return "\n".join(lines)

    def drain_for_prompt(
        self,
        *,
        limit: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        """Drain up to ``limit`` mentions into a prompt block bounded by ``max_chars``.

        Messages are selected oldest-first.  A message that would push the
        block past the character budget is left pending, unless it is the
        first message -- then its text is truncated so one oversized mention
        can never wedge the inbox.  Drained messages never render twice.
        """

        cap_limit = self.max_pending if limit is None else max(1, int(limit))
        cap_chars = self.max_prompt_chars if max_chars is None else max(1, int(max_chars))
        with self._lock:
            candidates = [item for item in self._inbox if item.message_id not in self._drained][
                :cap_limit
            ]
        selected: list[RadioMessage] = []
        lines: list[str] = ["Radio updates:"]
        used = len(lines[0])
        for message in candidates:
            line = self._format_line(message)
            cost = 1 + len(line)  # newline + line
            if used + cost <= cap_chars:
                lines.append(line)
                selected.append(message)
                used += cost
                continue
            if not selected:
                remaining = cap_chars - used - 1
                if remaining <= 0:
                    break
                lines.append(_truncate(line, remaining))
                selected.append(message)
            break
        if not selected:
            return ""
        with self._lock:
            self._drained.update(message.message_id for message in selected)
        return "\n".join(lines)

    def _format_line(self, message: RadioMessage) -> str:
        label = "thread" if message.kind == KIND_THREAD else message.kind
        parent = str(message.metadata.get("parent_id") or "").strip()
        suffix = f" (re {parent[:12]})" if parent else ""
        body = " ".join(message.text.split())
        return f"- [{label}] {message.node_id}{suffix}: {body}"


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters, marking the cut when possible."""

    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _iter_texts(values: Iterable[Any]) -> list[str]:
    """Small helper used by tests/adapters: coerce values to clean strings."""

    texts: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            texts.append(text)
    return texts
