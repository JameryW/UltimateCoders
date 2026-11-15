"""AST-anchored compact attempt evolution.

Replay of failed/rescued attempt pairs is most useful when a reader can see
which symbols an attempt touched across rounds instead of one raw diff per
round. This module compacts the ``patches`` of ``AttemptRound.to_dict()``
shaped rounds into :class:`EvolutionChange` records:

* unified diff ``-``/``+`` lines are parsed (header lines ignored);
* adjacent removals/additions become ``modified`` pairs, leftovers become
  ``added``/``removed`` changes;
* each change is anchored to the nearest enclosing Python class/function
  (:func:`anchor_symbol`), falling back to ``path::line`` when the source is
  unreadable or not Python;
* a later round that adds back a previous old value and removes the previous
  new value for the same path marks the earlier change ``reverted`` and the
  later round is recorded on :attr:`EvolutionChain.reverted_rounds`.

Feature flag: ``UC_AST_EVOLUTION``.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

__all__ = [
    "EVOLUTION_ENABLED_ENV",
    "MAX_STORED_CHAINS",
    "AttemptEvolutionStore",
    "EvolutionChain",
    "EvolutionChange",
    "anchor_symbol",
    "compact_evolution",
    "evolution_enabled",
]

EVOLUTION_ENABLED_ENV = "UC_AST_EVOLUTION"
MAX_STORED_CHAINS = 200

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_CHANGE_KINDS = frozenset({"added", "removed", "modified"})
_DEF_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_HUNK_PATTERN = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)
_VALUE_RENDER_LIMIT = 80


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class EvolutionChange:
    """One added/removed/modified value inside one attempt round."""

    path: str
    symbol: str
    kind: str
    old: str
    new: str
    round_index: int
    reverted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _text(self.path))
        object.__setattr__(self, "symbol", _text(self.symbol))
        kind = _text(self.kind).strip().lower()
        object.__setattr__(self, "kind", kind if kind in _CHANGE_KINDS else "modified")
        object.__setattr__(self, "old", _text(self.old))
        object.__setattr__(self, "new", _text(self.new))
        object.__setattr__(self, "round_index", max(0, _as_int(self.round_index)))
        object.__setattr__(self, "reverted", bool(self.reverted))


def _coerce_change(item: Any) -> EvolutionChange | None:
    if isinstance(item, EvolutionChange):
        return item
    if not isinstance(item, Mapping):
        return None
    return EvolutionChange(
        path=item.get("path"),
        symbol=item.get("symbol"),
        kind=item.get("kind", "modified"),
        old=item.get("old"),
        new=item.get("new"),
        round_index=item.get("round_index", 0),
        reverted=item.get("reverted", False),
    )


@dataclass(frozen=True)
class EvolutionChain:
    """Compacted evolution of one subtask across attempt rounds."""

    task_id: str = ""
    subtask_id: str = ""
    changes: tuple[EvolutionChange, ...] = ()
    rounds: int = 0
    reverted_rounds: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id))
        object.__setattr__(self, "subtask_id", _text(self.subtask_id))
        changes: list[EvolutionChange] = []
        for item in self.changes or ():
            change = _coerce_change(item)
            if change is not None:
                changes.append(change)
        object.__setattr__(self, "changes", tuple(changes))
        object.__setattr__(self, "rounds", max(0, _as_int(self.rounds)))
        rounds: set[int] = set()
        for item in self.reverted_rounds or ():
            rounds.add(max(0, _as_int(item)))
        object.__setattr__(self, "reverted_rounds", tuple(sorted(rounds)))

    @property
    def file_count(self) -> int:
        """How many distinct paths the chain touched."""

        return len({change.path for change in self.changes if change.path})

    def render(self, max_changes: int = 20) -> str:
        """Render at most ``max_changes`` one-line change summaries.

        Values are clipped to 80 characters. Reverted changes are prefixed
        with ``← reverted vN`` where ``N`` is the 1-based display version of
        the recorded reversion round.
        """

        limit = max(0, _as_int(max_changes))
        reverted_rounds = tuple(sorted(self.reverted_rounds))
        lines: list[str] = []
        for change in self.changes[:limit]:
            if change.kind == "added":
                body = f"+ {_clip(change.new)}"
            elif change.kind == "removed":
                body = f"- {_clip(change.old)}"
            else:
                body = f"~ {_clip(change.old)} -> {_clip(change.new)}"
            if change.reverted:
                version = _reversion_version(change, reverted_rounds)
                lines.append(f"← reverted v{version} {body}")
            else:
                lines.append(body)
        return "\n".join(lines)


def _clip(value: str) -> str:
    text = _text(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= _VALUE_RENDER_LIMIT:
        return text
    return text[:_VALUE_RENDER_LIMIT]


def _reversion_version(change: EvolutionChange, reverted_rounds: tuple[int, ...]) -> int:
    later = [
        round_index
        for round_index in reverted_rounds
        if round_index > change.round_index
    ]
    if later:
        selected = min(later)
    elif reverted_rounds:
        selected = max(reverted_rounds)
    else:
        selected = change.round_index
    return selected + 1


# ---------------------------------------------------------------------------
# AST anchoring
# ---------------------------------------------------------------------------


def _symbol_chain(tree: ast.AST, line: int) -> list[str]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    best: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, _DEF_NODES):
            continue
        start = getattr(node, "lineno", 0) or 0
        end = getattr(node, "end_lineno", start) or start
        if not (start <= line <= end):
            continue
        chain: list[str] = []
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, _DEF_NODES):
                chain.append(current.name)
            current = parents.get(current)
        chain.reverse()
        if len(chain) > len(best):
            best = chain
    return best


def anchor_symbol(path: str, line: int) -> str:
    """Return ``path::Class::func`` for the definition enclosing ``line``.

    The source file is read from ``path`` (relative to the current working
    directory) only for ``.py`` files. Missing/unreadable/invalid sources and
    non-Python paths fall back to ``path::line``; this function never raises.
    """

    target_path = _text(path)
    target_line = _as_int(line)
    fallback = f"{target_path}::{target_line}"
    if not target_path or target_line < 1:
        return fallback
    if not target_path.lower().endswith(".py"):
        return fallback
    try:
        source = Path(target_path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        chain = _symbol_chain(tree, target_line)
    except Exception:
        return fallback
    if not chain:
        return fallback
    return "::".join([target_path, *chain])


# ---------------------------------------------------------------------------
# Unified diff parsing
# ---------------------------------------------------------------------------


def _diff_changes(path: str, diff: str) -> list[tuple[str, str, str, int]]:
    """Parse one file diff into ``(kind, old, new, anchor_line)`` tuples."""

    changes: list[tuple[str, str, str, int]] = []
    removed: list[tuple[str, int]] = []
    added: list[tuple[str, int]] = []
    old_line = 1
    new_line = 1
    in_hunk = False

    def flush() -> None:
        nonlocal removed, added
        pairs = min(len(removed), len(added))
        for position in range(pairs):
            old_value, _old_anchor = removed[position]
            new_value, new_anchor = added[position]
            changes.append(("modified", old_value, new_value, max(1, new_anchor)))
        for old_value, old_anchor in removed[pairs:]:
            changes.append(("removed", old_value, "", max(1, old_anchor)))
        for new_value, new_anchor in added[pairs:]:
            changes.append(("added", "", new_value, max(1, new_anchor)))
        removed = []
        added = []

    for raw_line in diff.splitlines():
        if raw_line.startswith("@@"):
            flush()
            in_hunk = True
            match = _HUNK_PATTERN.match(raw_line)
            if match:
                old_line = int(match.group(1))
                new_line = int(match.group(3))
            continue
        if not in_hunk and (raw_line.startswith("---") or raw_line.startswith("+++")):
            flush()
            continue
        if raw_line.startswith("-"):
            if added:
                flush()
            removed.append((raw_line[1:], new_line))
            old_line += 1
            continue
        if raw_line.startswith("+"):
            added.append((raw_line[1:], new_line))
            new_line += 1
            continue
        if raw_line.startswith("\\"):
            continue
        if raw_line.startswith(" "):
            flush()
            old_line += 1
            new_line += 1
            continue
        if raw_line == "":
            flush()
            continue
        flush()
    flush()
    return changes


def _value_sets(
    changes: list[EvolutionChange],
) -> tuple[dict[int, dict[str, set[str]]], dict[int, dict[str, set[str]]]]:
    removed: dict[int, dict[str, set[str]]] = {}
    added: dict[int, dict[str, set[str]]] = {}
    for change in changes:
        if change.kind in ("removed", "modified"):
            removed.setdefault(change.round_index, {}).setdefault(
                change.path, set()
            ).add(change.old)
        if change.kind in ("added", "modified"):
            added.setdefault(change.round_index, {}).setdefault(
                change.path, set()
            ).add(change.new)
    return removed, added


def _reverting_round(
    change: EvolutionChange,
    removed_by_round: dict[int, dict[str, set[str]]],
    added_by_round: dict[int, dict[str, set[str]]],
) -> int | None:
    later_rounds = sorted(set(removed_by_round) | set(added_by_round))
    for later in later_rounds:
        if later <= change.round_index:
            continue
        removed = removed_by_round.get(later, {}).get(change.path, set())
        added = added_by_round.get(later, {}).get(change.path, set())
        if change.kind == "added":
            if change.new and change.new in removed:
                return later
        elif change.kind == "removed":
            if change.old and change.old in added:
                return later
        elif change.old != change.new:
            if change.new and change.new in removed and change.old in added:
                return later
    return None


def compact_evolution(
    rounds: Sequence[Mapping[str, Any]],
    *,
    task_id: str = "",
    subtask_id: str = "",
) -> EvolutionChain:
    """Compact ``AttemptRound.to_dict()``-shaped rounds into a chain.

    ``round_index`` is the zero-based position of the round in ``rounds``
    (matching the repository's zero-based attempt numbering). Changes are
    ordered by round, path and diff line order. Reversion detection only
    compares values within the same path.
    """

    records: list[Mapping[str, Any]] = []
    if rounds is not None:
        try:
            candidates = tuple(rounds)
        except TypeError:
            candidates = ()
        for item in candidates:
            if isinstance(item, Mapping):
                records.append(item)

    changes: list[EvolutionChange] = []
    for round_index, round_data in enumerate(records):
        patches = round_data.get("patches") or ()
        if isinstance(patches, Mapping):
            patches = (patches,)
        try:
            patch_items = tuple(patches)
        except TypeError:
            patch_items = ()
        for patch in patch_items:
            if not isinstance(patch, Mapping):
                continue
            path = _text(patch.get("path")).strip()
            diff = _text(patch.get("diff"))
            if not path or not diff.strip():
                continue
            for kind, old, new, line in _diff_changes(path, diff):
                changes.append(
                    EvolutionChange(
                        path=path,
                        symbol=anchor_symbol(path, line),
                        kind=kind,
                        old=old,
                        new=new,
                        round_index=round_index,
                    )
                )

    changes.sort(key=lambda change: (change.round_index, change.path))
    removed_by_round, added_by_round = _value_sets(changes)
    reverted_rounds: set[int] = set()
    marked: list[EvolutionChange] = []
    for change in changes:
        later = _reverting_round(change, removed_by_round, added_by_round)
        if later is None:
            marked.append(change)
        else:
            marked.append(replace(change, reverted=True))
            reverted_rounds.add(later)

    return EvolutionChain(
        task_id=task_id,
        subtask_id=subtask_id,
        changes=tuple(marked),
        rounds=len(records),
        reverted_rounds=tuple(sorted(reverted_rounds)),
    )


class AttemptEvolutionStore:
    """Bounded in-memory store of compacted evolution chains."""

    def __init__(self, max_chains: int = MAX_STORED_CHAINS) -> None:
        self._limit = max(1, _as_int(max_chains))
        self._chains: dict[tuple[str, str], EvolutionChain] = {}

    def __len__(self) -> int:
        return len(self._chains)

    def record(self, chain: EvolutionChain) -> None:
        """Store (or replace) the chain for its task/subtask pair."""

        if not isinstance(chain, EvolutionChain):
            raise TypeError("chain must be an EvolutionChain")
        key = (chain.task_id, chain.subtask_id)
        self._chains.pop(key, None)
        self._chains[key] = chain
        while len(self._chains) > self._limit:
            self._chains.pop(next(iter(self._chains)))

    def get(self, task_id: str, subtask_id: str) -> EvolutionChain | None:
        return self._chains.get((_text(task_id), _text(subtask_id)))

    def render(self, task_id: str, subtask_id: str) -> str:
        """Rendered chain for the pair, or an empty string when absent."""

        chain = self.get(task_id, subtask_id)
        return chain.render() if chain is not None else ""


def evolution_enabled() -> bool:
    """Whether ``UC_AST_EVOLUTION`` enables compact evolution capture."""

    value = os.environ.get(EVOLUTION_ENABLED_ENV, "")
    return value.strip().lower() in _TRUTHY_VALUES
