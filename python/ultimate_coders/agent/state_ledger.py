"""Explicit run state for the local harness (ledger item #80, production wiring).

``search/state_filter.py`` keeps the deterministic half of the #80 idea: a
model may only *parse* an event, while a rule engine owns the keyed state.  This
module is the caller the ledger item was missing.  The local harness applies one
typed event per observable fact — a file was written, a command exited with a
code — and reads a bounded snapshot instead of re-deriving "what has happened so
far" from the whole transcript.

The state is deliberately small and deterministic:

* ``file:<path>`` holds ``written`` for every path the run touched.
* ``command:last`` holds the last command that was started.
* ``command:<digest>`` holds that command's exit code.

Repeat writes with no intervening success are the one judgement the ledger
makes on its own: rewriting the same file three times without a passing command
in between is thrash, and the harness surfaces it once per path per checkpoint.
That signal is observation only — it never blocks a tool.

The ledger is opt-in and off by default: ``UC_STATE_LEDGER`` must be truthy and
``UC_STATE_FILTER`` must be on, because the ledger is a consumer of the filter.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from ultimate_coders.search.state_filter import (
    ExplicitStateFilter,
    ParsedEvent,
    state_filter_enabled,
)

__all__ = [
    "STATE_LEDGER_ENV",
    "MAX_KEYS",
    "THRASH_THRESHOLD",
    "MAX_RENDER_CHARS",
    "ThrashWarning",
    "StateLedger",
    "state_ledger_enabled",
]

STATE_LEDGER_ENV = "UC_STATE_LEDGER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MAX_KEYS = 64
THRASH_THRESHOLD = 3
MAX_RENDER_CHARS = 600
_MAX_VALUE = 80
_DIGEST_LEN = 12


def state_ledger_enabled() -> bool:
    """True only when both the ledger flag and the state filter are on."""
    if os.environ.get(STATE_LEDGER_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return state_filter_enabled()


@dataclass(frozen=True)
class ThrashWarning:
    """A path rewritten repeatedly with no successful command in between."""

    step: int
    path: str
    writes: int

    @property
    def message(self) -> str:
        return (
            f"{self.path} was rewritten {self.writes} times without a passing "
            "command in between; stop rewriting it and run the verification first"
        )

    def to_dict(self) -> dict[str, object]:
        return {"step": self.step, "path": self.path, "writes": self.writes}


def _normalise_path(path: str) -> str:
    cleaned = str(path).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned:
        raise ValueError("path must not be empty")
    return cleaned


def _clip(value: str) -> str:
    text = " ".join(str(value).split())
    return text[:_MAX_VALUE]


class StateLedger:
    """Bounded keyed state for one harness run."""

    def __init__(
        self,
        max_keys: int = MAX_KEYS,
        thrash_threshold: int = THRASH_THRESHOLD,
    ) -> None:
        if thrash_threshold < 2:
            raise ValueError("thrash_threshold must be at least 2")
        self._state = ExplicitStateFilter(max_keys=max_keys)
        self._threshold = thrash_threshold
        self._writes: dict[str, int] = {}
        self._thrashing: dict[str, int] = {}
        self._warned: set[str] = set()

    @property
    def thrash_threshold(self) -> int:
        return self._threshold

    def record_write(self, step: int, path: str) -> ThrashWarning | None:
        """Apply a write event and report thrash the first time it is seen."""
        rel = _normalise_path(path)
        self._state.apply_parsed(ParsedEvent(f"file:{rel}", "written", step))
        count = self._writes.get(rel, 0) + 1
        self._writes[rel] = count
        if count < self._threshold or rel in self._warned:
            return None
        self._warned.add(rel)
        self._thrashing[rel] = count
        return ThrashWarning(step=step, path=rel, writes=count)

    def record_command(self, step: int, command: str, exit_code: int) -> None:
        """Apply a command event; a passing command clears write counts."""
        text = _clip(command)
        if text:
            self._state.apply_parsed(ParsedEvent("command:last", text, step))
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
            self._state.apply_parsed(
                ParsedEvent(f"command:{digest}", str(int(exit_code)), step)
            )
        if int(exit_code) == 0:
            self._writes.clear()
            self._thrashing.clear()
            self._warned.clear()

    def thrashing(self) -> tuple[str, ...]:
        return tuple(sorted(self._thrashing))

    def entries(self) -> dict[str, str]:
        return {
            key: entry.value for key, entry in self._state.snapshot().items()
        }

    def render(self) -> str:
        lines = [f"- {key} = {value}" for key, value in self.entries().items()]
        if self._thrashing:
            lines.append("- thrash: " + ", ".join(self.thrashing()))
        text = "\n".join(lines)
        if len(text) <= MAX_RENDER_CHARS:
            return text
        return text[: MAX_RENDER_CHARS - 3].rstrip() + "..."

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [
                entry.to_dict() for entry in self._state.snapshot().values()
            ],
            "stats": self._state.stats.to_dict(),
            "thrash": list(self.thrashing()),
            "thrash_threshold": self._threshold,
        }
