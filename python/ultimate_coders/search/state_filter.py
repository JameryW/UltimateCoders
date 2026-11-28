"""Rate-limited explicit state filtering (ledger item #80).

The LLM is only allowed to parse an event line into a typed delta. A
deterministic rule engine applies the delta to an explicit keyed state. This
keeps long event streams from drifting and prevents model-authored updates
from silently overwriting durable state.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "STATE_FILTER_ENV",
    "StateFilterError",
    "ParsedEvent",
    "StateEntry",
    "StateTransition",
    "StateFilterStats",
    "ExplicitStateFilter",
    "parse_event_line",
    "state_filter_enabled",
]

STATE_FILTER_ENV = "UC_STATE_FILTER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def state_filter_enabled() -> bool:
    return os.environ.get(STATE_FILTER_ENV, "").strip().lower() in _TRUTHY


class StateFilterError(ValueError):
    """Raised when a parsed event cannot be applied deterministically."""


@dataclass(frozen=True)
class ParsedEvent:
    key: str
    value: str | None
    timestamp: int
    dependency_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise StateFilterError("event key must not be empty")
        if self.timestamp < 0:
            raise StateFilterError("timestamp must be non-negative")
        object.__setattr__(
            self,
            "dependency_keys",
            tuple(dict.fromkeys(str(item) for item in self.dependency_keys if str(item))),
        )


@dataclass(frozen=True)
class StateEntry:
    key: str
    value: str
    timestamp: int
    dependencies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "value": self.value,
            "timestamp": self.timestamp,
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True)
class StateTransition:
    key: str
    value: str | None
    timestamp: int
    action: str = "set"

    def __post_init__(self) -> None:
        if self.action not in {"set", "delete"}:
            raise StateFilterError(f"unknown transition action: {self.action}")
        if self.action == "set" and self.value is None:
            raise StateFilterError("set transition requires a value")


@dataclass
class StateFilterStats:
    applied: int = 0
    deleted: int = 0
    rejected_stale: int = 0
    evicted: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "applied": self.applied,
            "deleted": self.deleted,
            "rejected_stale": self.rejected_stale,
            "evicted": self.evicted,
        }


class ExplicitStateFilter:
    """A bounded key/value state machine with deterministic updates."""

    def __init__(self, max_keys: int = 128) -> None:
        if max_keys < 1:
            raise StateFilterError("max_keys must be at least 1")
        self._max_keys = max_keys
        self._state: dict[str, StateEntry] = {}
        self._stats = StateFilterStats()

    @property
    def max_keys(self) -> int:
        return self._max_keys

    @property
    def stats(self) -> StateFilterStats:
        return self._stats

    def apply(self, transition: StateTransition) -> bool:
        current = self._state.get(transition.key)
        if current is not None and transition.timestamp < current.timestamp:
            self._stats.rejected_stale += 1
            return False
        if transition.action == "delete":
            if current is not None:
                del self._state[transition.key]
            self._stats.deleted += 1
            return True
        self._state[transition.key] = StateEntry(
            key=transition.key,
            value=str(transition.value),
            timestamp=transition.timestamp,
        )
        self._stats.applied += 1
        self._evict_if_needed()
        return True

    def apply_parsed(self, event: ParsedEvent) -> bool:
        action = "delete" if event.value is None else "set"
        ok = self.apply(StateTransition(event.key, event.value, event.timestamp, action))
        if ok and event.dependency_keys:
            entry = self._state.get(event.key)
            if entry is not None:
                self._state[event.key] = StateEntry(
                    entry.key, entry.value, entry.timestamp, event.dependency_keys
                )
        return ok

    def apply_many(self, events: Iterable[ParsedEvent]) -> int:
        return sum(1 for event in events if self.apply_parsed(event))

    def get(self, key: str) -> StateEntry | None:
        return self._state.get(key)

    def snapshot(self) -> dict[str, StateEntry]:
        return dict(sorted(self._state.items()))

    def dependencies(self, key: str) -> tuple[str, ...]:
        entry = self._state.get(key)
        return entry.dependencies if entry else ()

    def _evict_if_needed(self) -> None:
        while len(self._state) > self._max_keys:
            oldest = min(
                self._state.values(),
                key=lambda entry: (entry.timestamp, entry.key),
            )
            del self._state[oldest.key]
            self._stats.evicted += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "max_keys": self._max_keys,
            "entries": [entry.to_dict() for entry in self.snapshot().values()],
            "stats": self._stats.to_dict(),
        }


def parse_event_line(line: str) -> ParsedEvent:
    """Parse a single ``key=value@timestamp`` or ``key=delete@timestamp`` line."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        raise StateFilterError("event line is empty or a comment")
    if "@" not in raw:
        raise StateFilterError("event line must end with @<timestamp>")
    body, timestamp_text = raw.rsplit("@", 1)
    try:
        timestamp = int(timestamp_text)
    except ValueError as exc:
        raise StateFilterError("event timestamp must be an integer") from exc
    if "=" not in body:
        raise StateFilterError("event line must contain '='")
    key, value = body.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        raise StateFilterError("event key must not be empty")
    return ParsedEvent(key=key, value=None if value == "delete" else value, timestamp=timestamp)
