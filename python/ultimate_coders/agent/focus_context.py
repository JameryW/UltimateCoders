"""Focus resumption context for interleaved subtasks (V29 #135).

The ledger item (**Focus-Task Resumption**, ``arXiv:2610.35094``) measured what
happens when one repository carries several unfinished tasks at once: agents
lose 11.5 percentage points on the main task because they mix requirements and
state across tasks, and a relevance-filtered state summary recovers part of the
loss. ``python/ultimate_coders/architecture/focus_resumption.py`` implements the
per-task cards and the history filter, but nothing in the worker ever built a
card, so the module was dead weight.

This module adds the missing production caller. It keeps a process-local
registry of the subtasks that are currently in flight inside one task graph:

* a subtask **begins** -> its card (description + its declared file
  constraints) is registered before its prompt is assembled;
* a later subtask of the same graph **assembles its context** -> every still
  open card is rendered as an additive evidence block, so the agent knows what
  else is being changed in this repository right now;
* a subtask **finishes** -> its card is retired, so the block shrinks.

The block is advisory by construction: it never rewrites requirements, never
claims the other task is broken, and never removes work from the prompt. Cards
are filtered the way the paper prescribes - only cards that share vocabulary
with the current description are spelled out; the rest collapse into one
"unrelated areas" line so the block cannot crowd out the task itself.

Opt-in and process-local: ``UC_FOCUS_CONTEXT`` plus ``UC_FOCUS_RESUMPTION``
must both be on (the flag guards a cache, and the underlying cards are the
ledger feature). Nothing here performs I/O, writes files, or calls a model.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ultimate_coders.architecture.focus_resumption import focus_resumption_enabled

__all__ = [
    "FOCUS_CONTEXT_ENV",
    "MAX_CARDS",
    "MAX_LISTED",
    "FocusEntry",
    "SubtaskFocus",
    "begin_subtask",
    "finish_subtask",
    "focus_block",
    "focus_context_enabled",
    "reset_focus_context",
    "focus_state",
]

FOCUS_CONTEXT_ENV = "UC_FOCUS_CONTEXT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MAX_CARDS = 16
MAX_LISTED = 3
MAX_RENDER_CHARS = 900
_MIN_TERM = 4
_MAX_DESCRIPTION = 160
_STALE_GENERATIONS = 64

_WORD = re.compile(r"[A-Za-z0-9_]{4,}")


def focus_context_enabled() -> bool:
    """True when both this cache and the underlying cards are enabled."""

    if os.environ.get(FOCUS_CONTEXT_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return focus_resumption_enabled()


@dataclass(frozen=True)
class FocusEntry:
    """One subtask that is still in flight inside a task graph."""

    scope: str
    subtask_id: str
    description: str
    files: tuple[str, ...] = ()
    generation: int = 0

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ValueError("scope must not be empty")
        if not self.subtask_id.strip():
            raise ValueError("subtask_id must not be empty")

    def line(self) -> str:
        """One bounded line naming the work and its files."""

        files = ", ".join(self.files[:3]) or "-"
        return f"- [{self.subtask_id}] {self.description} (files: {files})"

    def to_dict(self) -> dict[str, object]:
        return {
            "subtask_id": self.subtask_id,
            "description": self.description,
            "files": list(self.files),
            "generation": self.generation,
        }


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3].rstrip() + "..."


def _terms(text: str) -> set[str]:
    return {word.lower() for word in _WORD.findall(text)}


class SubtaskFocus:
    """Registry of in-flight subtasks, keyed by graph scope then subtask id."""

    def __init__(
        self,
        max_cards: int = MAX_CARDS,
        stale_generations: int = _STALE_GENERATIONS,
    ) -> None:
        if max_cards <= 0:
            raise ValueError("max_cards must be positive")
        if stale_generations <= 0:
            raise ValueError("stale_generations must be positive")
        self._max_cards = max_cards
        self._stale_generations = stale_generations
        self._entries: dict[tuple[str, str], FocusEntry] = {}
        self._generation = 0

    def begin(
        self,
        scope: str,
        subtask_id: str,
        description: str,
        *,
        files: tuple[str, ...] = (),
    ) -> FocusEntry | None:
        """Register (or refresh) the card for a starting subtask."""

        if not scope.strip() or not subtask_id.strip():
            return None
        key = (scope, subtask_id)
        if key not in self._entries and len(self._entries) >= self._max_cards:
            # Never fail a subtask because the cache is full; drop the oldest.
            oldest = min(self._entries.items(), key=lambda item: item[1].generation)
            self._entries.pop(oldest[0], None)
        self._generation += 1
        entry = FocusEntry(
            scope=scope,
            subtask_id=subtask_id,
            description=_clip(description, _MAX_DESCRIPTION),
            files=tuple(dict.fromkeys(item for item in files if item.strip())),
            generation=self._generation,
        )
        self._entries[key] = entry
        self._prune()
        return entry

    def finish(self, scope: str, subtask_id: str) -> int:
        """Retire one card; returns how many cards remain open in the scope."""

        self._entries.pop((scope, subtask_id), None)
        return len(self.open_tasks(scope))

    def open_tasks(
        self,
        scope: str,
        *,
        exclude: str = "",
    ) -> tuple[FocusEntry, ...]:
        """Open cards of one scope, most recent first, minus ``exclude``."""

        entries = [
            entry
            for (entry_scope, _), entry in self._entries.items()
            if entry_scope == scope and entry.subtask_id != exclude
        ]
        entries.sort(key=lambda entry: (-entry.generation, entry.subtask_id))
        return tuple(entries)

    def render(
        self,
        scope: str,
        description: str,
        *,
        exclude: str = "",
        max_listed: int = MAX_LISTED,
    ) -> tuple[str, dict[str, object]] | None:
        """Render the additive "other work in flight" block for one subtask."""

        entries = self.open_tasks(scope, exclude=exclude)
        if not entries:
            return None
        wanted = _terms(description)
        relevant: list[FocusEntry] = []
        unrelated = 0
        for entry in entries:
            entry_terms = _terms(entry.description) | _terms(" ".join(entry.files))
            if wanted & entry_terms:
                relevant.append(entry)
            else:
                unrelated += 1
        if not relevant:
            text = (
                "Other unfinished work in the same task graph:\n"
                f"{unrelated} subtask(s) are changing unrelated files; "
                "keep your change scoped to your own targets."
            )
            return _fit(text, {"open": len(entries), "listed": 0, "unrelated": unrelated})
        lines = [
            "Other unfinished work in the same task graph (do not revert it):",
            *(entry.line() for entry in relevant[:max_listed]),
        ]
        if len(relevant) > max_listed:
            lines.append(f"- ... and {len(relevant) - max_listed} more related subtask(s)")
        if unrelated:
            lines.append(
                f"{unrelated} further subtask(s) touch unrelated areas; "
                "keep your change scoped to your own targets."
            )
        return _fit(
            "\n".join(lines),
            {
                "open": len(entries),
                "listed": min(len(relevant), max_listed),
                "related": len(relevant),
                "unrelated": unrelated,
            },
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "open": len(self._entries),
            "generation": self._generation,
            "scopes": sorted({scope for scope, _ in self._entries}),
        }

    def reset(self) -> None:
        self._entries.clear()
        self._generation = 0

    def _prune(self) -> None:
        cutoff = self._generation - self._stale_generations
        for key, entry in list(self._entries.items()):
            if entry.generation < cutoff:
                self._entries.pop(key, None)


def _fit(text: str, meta: dict[str, object]) -> tuple[str, dict[str, object]]:
    if len(text) > MAX_RENDER_CHARS:
        text = text[: MAX_RENDER_CHARS - 3].rstrip() + "..."
    return text, meta


_REGISTRY: SubtaskFocus | None = None


def _registry() -> SubtaskFocus:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SubtaskFocus()
    return _REGISTRY


def reset_focus_context() -> None:
    """Drop every registered card (tests and between dispatch batches)."""

    global _REGISTRY
    if _REGISTRY is not None:
        _REGISTRY.reset()


def _scope_of(subtask: object) -> str:
    return str(getattr(subtask, "parent_id", "") or "")


def begin_subtask(subtask: object) -> FocusEntry | None:
    """Register the card for a subtask that is about to assemble its prompt."""

    if not focus_context_enabled():
        return None
    scope = _scope_of(subtask)
    subtask_id = str(getattr(subtask, "id", "") or "")
    description = str(getattr(subtask, "description", "") or "")
    if not scope or not subtask_id:
        return None
    constraints = getattr(subtask, "file_constraints", None) or ()
    files = tuple(str(item) for item in constraints if str(item).strip())
    return _registry().begin(scope, subtask_id, description, files=files)


def finish_subtask(subtask: object) -> dict[str, object] | None:
    """Retire the card and report how much work is still in flight."""

    if not focus_context_enabled():
        return None
    scope = _scope_of(subtask)
    subtask_id = str(getattr(subtask, "id", "") or "")
    if not scope or not subtask_id:
        return None
    remaining = _registry().finish(scope, subtask_id)
    state = _registry().to_dict()
    return {
        "phase": "focus resumption",
        "percent": 0,
        "focus_cleared": subtask_id,
        "focus_open": remaining,
        "focus_scopes": state["scopes"],
    }


def focus_block(subtask: object) -> tuple[str, dict[str, object]] | None:
    """Additive context block listing the other work in flight."""

    if not focus_context_enabled():
        return None
    scope = _scope_of(subtask)
    subtask_id = str(getattr(subtask, "id", "") or "")
    if not scope or not subtask_id:
        return None
    description = str(getattr(subtask, "description", "") or "")
    return _registry().render(scope, description, exclude=subtask_id)


def focus_state() -> dict[str, object]:
    """Bounded JSON-ready registry state (counts and scopes only)."""

    if _REGISTRY is None:
        return {"open": 0, "generation": 0, "scopes": []}
    return _REGISTRY.to_dict()
