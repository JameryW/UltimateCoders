"""SWE-Shift guard: mid-execution workspace shift reconciliation (#26).

Ledger item #26 adapts SWE-Shift (arXiv:2610.32196): a long-running coding
attempt can have its world change underneath it — a collaborator commit, a
public interface change, a rebound tool session, a revised requirement, or
stale evidence.  The guard implements the three-step recovery protocol:

1. change notification (freeze writes, emit a structured event),
2. read-only refresh,
3. history reset plus a mandatory fresh observation before the first edit.

Feature flag: ``UC_SHIFT_GUARD`` (default off).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "UC_SHIFT_GUARD",
    "ShiftKind",
    "WorkspaceEpoch",
    "WorkspaceSnapshot",
    "ShiftEvent",
    "ShiftPhase",
    "RecoveryPlan",
    "ShiftGuard",
    "snapshot_workspace",
    "shift_guard_enabled",
]

UC_SHIFT_GUARD = "UC_SHIFT_GUARD"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEFAULT_EVENT_CAPACITY = 512
_GIT_TIMEOUT_S = 5.0


def shift_guard_enabled() -> bool:
    """Return whether mid-execution shift guarding is enabled."""

    return os.environ.get(UC_SHIFT_GUARD, "").strip().lower() in _TRUTHY


class ShiftKind(str, Enum):
    COLLABORATOR_COMMIT = "collaborator_commit"
    INTERFACE_CHANGE = "interface_change"
    TOOL_SESSION_REBIND = "tool_session_rebind"
    REQUIREMENT_REVISION = "requirement_revision"
    STALE_EVIDENCE = "stale_evidence"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


class ShiftPhase(str, Enum):
    STABLE = "stable"
    FROZEN = "frozen"
    REFRESH_PENDING = "refresh_pending"
    HISTORY_RESET_REQUIRED = "history_reset_required"
    AWAITING_FRESH_OBSERVATION = "awaiting_fresh_observation"
    READY = "ready"


@dataclass(frozen=True)
class WorkspaceEpoch:
    """Identity of the workspace as observed by the agent."""

    git_head: str = ""
    tool_schema_epoch: int = 0
    requirement_revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "git_head", str(self.git_head).strip())
        object.__setattr__(self, "tool_schema_epoch", int(self.tool_schema_epoch))
        object.__setattr__(self, "requirement_revision", int(self.requirement_revision))

    def to_dict(self) -> dict[str, object]:
        return {
            "git_head": self.git_head,
            "tool_schema_epoch": self.tool_schema_epoch,
            "requirement_revision": self.requirement_revision,
        }


@dataclass(frozen=True)
class ShiftEvent:
    kind: ShiftKind
    previous: WorkspaceEpoch
    current: WorkspaceEpoch
    changed_paths: tuple[str, ...] = ()
    added_symbols: tuple[str, ...] = ()
    removed_symbols: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "previous": self.previous.to_dict(),
            "current": self.current.to_dict(),
            "changed_paths": list(self.changed_paths),
            "added_symbols": list(self.added_symbols),
            "removed_symbols": list(self.removed_symbols),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    event: ShiftEvent
    read_only_refresh: bool
    history_reset: bool
    must_observe_paths: tuple[str, ...]
    preserved_symbols: tuple[str, ...]
    steps: tuple[str, ...]

    def observations_satisfied(self, observed: Iterable[str]) -> bool:
        observed_set = set(observed)
        if "*" in observed_set:
            return True
        if not self.must_observe_paths:
            return "*" in observed_set
        return all(path in observed_set for path in self.must_observe_paths)

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event.to_dict(),
            "read_only_refresh": self.read_only_refresh,
            "history_reset": self.history_reset,
            "must_observe_paths": list(self.must_observe_paths),
            "preserved_symbols": list(self.preserved_symbols),
            "steps": list(self.steps),
        }


def _normalise(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in items if str(item).strip()}))


def _classify(
    previous: WorkspaceEpoch,
    current: WorkspaceEpoch,
    added: tuple[str, ...],
    removed: tuple[str, ...],
) -> ShiftKind:
    if current.requirement_revision != previous.requirement_revision:
        return ShiftKind.REQUIREMENT_REVISION
    if current.tool_schema_epoch != previous.tool_schema_epoch:
        return ShiftKind.TOOL_SESSION_REBIND
    if added or removed:
        return ShiftKind.INTERFACE_CHANGE
    return ShiftKind.COLLABORATOR_COMMIT


@dataclass
class ShiftGuard:
    """Deterministic state machine for one agent attempt."""

    initial_epoch: WorkspaceEpoch = field(default_factory=WorkspaceEpoch)
    max_events: int = _DEFAULT_EVENT_CAPACITY
    _current: WorkspaceEpoch = field(init=False)
    _phase: ShiftPhase = field(init=False, default=ShiftPhase.STABLE)
    _pending: RecoveryPlan | None = field(init=False, default=None)
    _observed: set[str] = field(init=False, default_factory=set)
    _preserved: set[str] = field(init=False, default_factory=set)
    _events: list[ShiftEvent] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if self.max_events < 1:
            raise ValueError("max_events must be >= 1")
        self._current = self.initial_epoch
        self._events = []

    @property
    def current_epoch(self) -> WorkspaceEpoch:
        return self._current

    @property
    def phase(self) -> ShiftPhase:
        return self._phase

    @property
    def plan(self) -> RecoveryPlan | None:
        return self._pending

    @property
    def events(self) -> tuple[ShiftEvent, ...]:
        return tuple(self._events)

    @property
    def can_write(self) -> bool:
        return self._phase in (ShiftPhase.STABLE, ShiftPhase.READY)

    def observe_epoch(
        self,
        epoch: WorkspaceEpoch,
        *,
        changed_paths: Iterable[str] = (),
        added_symbols: Iterable[str] = (),
        removed_symbols: Iterable[str] = (),
        detail: str = "",
    ) -> ShiftEvent | None:
        """Observe an epoch snapshot; returns an event only when it changed."""

        if epoch == self._current:
            return None
        previous = self._current
        changed = _normalise(changed_paths)
        added = _normalise(added_symbols)
        removed = _normalise(removed_symbols)
        kind = _classify(previous, epoch, added, removed)
        self._preserved.update(added)
        self._current = epoch
        event = ShiftEvent(
            kind=kind,
            previous=previous,
            current=epoch,
            changed_paths=changed,
            added_symbols=added,
            removed_symbols=removed,
            detail=detail or kind.label,
        )
        self._begin(event)
        return event

    def observe_stale_evidence(self, detail: str, affected_paths: Iterable[str] = ()) -> ShiftEvent:
        """Record stale evidence without an epoch change."""

        event = ShiftEvent(
            kind=ShiftKind.STALE_EVIDENCE,
            previous=self._current,
            current=self._current,
            changed_paths=_normalise(affected_paths),
            detail=detail,
        )
        self._begin(event)
        return event

    def acknowledge_refresh(self) -> None:
        if self._phase is not ShiftPhase.FROZEN:
            raise ValueError("refresh acknowledgement is out of order")
        self._phase = ShiftPhase.REFRESH_PENDING

    def apply_history_reset(self) -> None:
        if self._phase is not ShiftPhase.REFRESH_PENDING:
            raise ValueError("history reset is out of order")
        if self._pending is None:
            raise ValueError("no shift recovery is pending")
        self._phase = ShiftPhase.HISTORY_RESET_REQUIRED

    def record_fresh_observation(self, path: str) -> bool:
        """Record one fresh observation; returns True when recovery completes."""

        if self._phase not in (
            ShiftPhase.HISTORY_RESET_REQUIRED,
            ShiftPhase.AWAITING_FRESH_OBSERVATION,
        ):
            return False
        cleaned = str(path).strip()
        if not cleaned:
            return False
        self._observed.add(cleaned)
        satisfied = (
            self._pending.observations_satisfied(self._observed)
            if self._pending is not None
            else True
        )
        self._phase = ShiftPhase.READY if satisfied else ShiftPhase.AWAITING_FRESH_OBSERVATION
        return satisfied

    def remaining_observations(self) -> tuple[str, ...]:
        if self._pending is None or self._pending.observations_satisfied(self._observed):
            return ()
        if not self._pending.must_observe_paths:
            return ("*",)
        return tuple(
            path for path in self._pending.must_observe_paths if path not in self._observed
        )

    def verify_symbols_preserved(self, removed_symbols: Iterable[str]) -> None:
        for symbol in removed_symbols:
            if str(symbol) in self._preserved:
                raise ValueError(f"preserved collaborator symbol was removed: {symbol}")

    def _begin(self, event: ShiftEvent) -> None:
        self._phase = ShiftPhase.FROZEN
        self._observed.clear()
        self._pending = RecoveryPlan(
            event=event,
            read_only_refresh=True,
            history_reset=True,
            must_observe_paths=event.changed_paths,
            preserved_symbols=tuple(sorted(self._preserved)),
            steps=(
                f"freeze writes after {event.kind.label}",
                "read-only refresh of affected state",
                "reset pre-change reasoning history",
                "require one fresh observation in the current epoch",
            ),
        )
        self._events.append(event)
        if len(self._events) > self.max_events:
            del self._events[: len(self._events) - self.max_events]


def summarize_shift(events: Iterable[ShiftEvent]) -> Mapping[str, int]:
    """Count events by kind for dashboards."""

    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind.value] = counts.get(event.kind.value, 0) + 1
    return counts


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """A git-derived workspace fingerprint used for mid-run shift detection."""

    epoch: WorkspaceEpoch
    changed_paths: tuple[str, ...] = ()
    commit: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "epoch": self.epoch.to_dict(),
            "changed_paths": list(self.changed_paths),
            "commit": self.commit,
        }


def _git(root: Path, *args: str) -> str | None:
    """Run one read-only git query; ``None`` when git or the repo is absent."""

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ("git", "-C", str(root), *args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _status_path(line: str) -> str:
    """Extract the path from one ``git status --porcelain`` line."""

    raw = line[3:] if len(line) > 3 else ""
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[1]
    return raw.strip().strip('"')


def snapshot_workspace(root: Path | str) -> WorkspaceSnapshot | None:
    """Snapshot HEAD plus the dirty path set for shift detection.

    The epoch packs the commit and a digest of the working-tree status, so a
    collaborator commit, a user edit, or a checkout each change it -- exactly
    what :meth:`ShiftGuard.observe_epoch` needs to notice.  The dirty paths
    become the files the agent must re-read before writing again.  Returns
    ``None`` outside a git repository so a caller can stay disabled instead of
    guessing.
    """

    path = Path(root)
    toplevel = _git(path, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    # Only guard the workspace the caller named.  A directory that merely lives
    # inside a parent repository must not inherit that repository's shifts.
    try:
        if os.path.normcase(Path(toplevel.strip()).as_posix()) != os.path.normcase(
            path.resolve().as_posix()
        ):
            return None
    except OSError:
        return None
    head = _git(path, "rev-parse", "HEAD")
    if head is None:
        return None
    commit = head.strip()
    status = _git(path, "status", "--porcelain=v1", "--untracked-files=no") or ""
    lines = [line.rstrip() for line in status.splitlines() if line.strip()]
    changed = tuple(dict.fromkeys(candidate for line in lines if (candidate := _status_path(line))))
    digest = hashlib.sha256(status.encode("utf-8")).hexdigest()[:12] if lines else ""
    epoch = WorkspaceEpoch(git_head=f"{commit}+{digest}" if digest else commit)
    return WorkspaceSnapshot(epoch=epoch, changed_paths=changed, commit=commit)
