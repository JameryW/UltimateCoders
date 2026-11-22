"""Sync-to-async promotion guard and Event Gate convergence notices.

A synchronous decision (an approval, a routing choice, a tool result) may be
answered quickly; when it is not, v9 promotes it to an asynchronous decision
instead of blocking the attempt forever.  This module owns that five-second
budget and the convergence notices the Event Gate consumes afterwards:

* :meth:`SyncAsyncPromoter.submit` records a pending decision (idempotent per
  ``decision_id``, so a retried submit keeps the original timestamp/payload);
* :meth:`SyncAsyncPromoter.check` / :meth:`SyncAsyncPromoter.promote_due`
  promote decisions whose elapsed time reached ``threshold_seconds``;
* :meth:`SyncAsyncPromoter.converge` records the eventual async outcome
  (``converged`` or ``rejected``);
* :meth:`SyncAsyncPromoter.notices` drains the bounded convergence notices
  (``promoted`` / ``converged`` / ``rejected``) exactly once.

Everything is in-process: no thread, timer or event bus is started, so tests
drive time through the injected ``now`` callable.  The feature flag
``UC_SYNC_ASYNC_PROMOTION`` gates the caller, not the class.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "ConvergenceNotice",
    "PendingDecision",
    "SyncAsyncPromoter",
    "promotion_enabled",
]

_LOGGER = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

PENDING = "pending"
PROMOTED = "promoted"
CONVERGED = "converged"
REJECTED = "rejected"

_STATUSES = frozenset({PENDING, PROMOTED, CONVERGED, REJECTED})
_TERMINAL = frozenset({CONVERGED, REJECTED})

#: Default sync budget before a decision is promoted to async.
DEFAULT_THRESHOLD_SECONDS = 5.0

#: Default ceiling on tracked decisions (and on retained notices).
DEFAULT_MAX_PENDING = 256

_PROMOTED_REASON = "promoted: sync threshold exceeded"
_CAPACITY_REASON = "rejected: sync-async pending capacity exceeded"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def promotion_enabled() -> bool:
    """Feature flag: sync-to-async decision promotion (default off)."""

    return _env_flag("UC_SYNC_ASYNC_PROMOTION")


@dataclass(frozen=True)
class PendingDecision:
    """One decision awaiting a synchronous answer."""

    decision_id: str
    payload: Mapping[str, Any]
    created_at: float
    status: str = PENDING
    reason: str = ""

    def __post_init__(self) -> None:
        decision_id = str(self.decision_id or "").strip()
        if not decision_id:
            raise ValueError("decision_id must be non-empty")
        object.__setattr__(self, "decision_id", decision_id)
        status = str(self.status or "").strip()
        if status not in _STATUSES:
            raise ValueError(f"unknown pending decision status {status!r}")
        created = float(self.created_at)
        if not math.isfinite(created):
            raise ValueError("created_at must be finite")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "payload", dict(self.payload or {}))
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "reason", str(self.reason or ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "payload": dict(self.payload),
            "created_at": float(self.created_at),
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ConvergenceNotice:
    """A status transition the Event Gate may publish."""

    decision_id: str
    status: str
    elapsed_seconds: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "status": self.status,
            "elapsed_seconds": float(self.elapsed_seconds),
            "reason": self.reason,
        }


class SyncAsyncPromoter:
    """Bounded, clock-injected sync-to-async promotion guard."""

    def __init__(
        self,
        threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
        now: Callable[[], float] = time.time,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        if isinstance(threshold_seconds, bool):
            raise ValueError("threshold_seconds must be a finite non-negative number")
        threshold = float(threshold_seconds)
        if not math.isfinite(threshold) or threshold < 0.0:
            raise ValueError("threshold_seconds must be non-negative")
        if isinstance(max_pending, bool):
            raise ValueError("max_pending must be positive")
        limit = int(max_pending)
        if limit <= 0:
            raise ValueError("max_pending must be positive")
        self._threshold = threshold
        self._now = now
        self._max_pending = limit
        self._lock = threading.RLock()
        self._decisions: dict[str, PendingDecision] = {}
        self._notices: deque[ConvergenceNotice] = deque(maxlen=limit)
        self._callbacks: list[Callable[[ConvergenceNotice], None]] = []

    @property
    def threshold_seconds(self) -> float:
        return self._threshold

    @property
    def max_pending(self) -> int:
        return self._max_pending

    def __len__(self) -> int:
        with self._lock:
            return len(self._decisions)

    def submit(
        self, decision_id: str, payload: Mapping[str, Any] | None = None
    ) -> PendingDecision:
        """Record a pending decision; a repeated id returns the original."""

        key = str(decision_id or "").strip()
        if not key:
            raise ValueError("decision_id must be non-empty")
        pending_notices: list[
            tuple[ConvergenceNotice, tuple[Callable[[ConvergenceNotice], None], ...]]
        ] = []
        with self._lock:
            existing = self._decisions.get(key)
            if existing is not None:
                return existing
            self._make_room(pending_notices)
            decision = PendingDecision(
                decision_id=key,
                payload=dict(payload or {}),
                created_at=float(self._now()),
            )
            self._decisions[key] = decision
        self._dispatch_notices(pending_notices)
        return decision

    def pending(self) -> tuple[PendingDecision, ...]:
        """Pending decisions, oldest first (ties broken by decision id)."""

        with self._lock:
            return tuple(
                sorted(
                    (
                        decision
                        for decision in self._decisions.values()
                        if decision.status == PENDING
                    ),
                    key=lambda decision: (decision.created_at, decision.decision_id),
                )
            )

    def check(self, decision_id: str) -> PendingDecision:
        """Promote a due decision, otherwise return its current state."""

        pending_notices: list[
            tuple[ConvergenceNotice, tuple[Callable[[ConvergenceNotice], None], ...]]
        ] = []
        with self._lock:
            decision = self._require(decision_id)
            if decision.status != PENDING:
                return decision
            if self._elapsed(decision) < self._threshold:
                return decision
            updated = self._settle(decision, PROMOTED, _PROMOTED_REASON, pending_notices)
        self._dispatch_notices(pending_notices)
        return updated

    def promote_due(self) -> tuple[PendingDecision, ...]:
        """Promote every pending decision whose sync budget is spent."""

        promoted: list[PendingDecision] = []
        pending_notices: list[
            tuple[ConvergenceNotice, tuple[Callable[[ConvergenceNotice], None], ...]]
        ] = []
        with self._lock:
            candidates = sorted(
                (
                    decision
                    for decision in self._decisions.values()
                    if decision.status == PENDING
                ),
                key=lambda decision: (decision.created_at, decision.decision_id),
            )
            for decision in candidates:
                current = self._decisions.get(decision.decision_id)
                if current is None or current.status != PENDING:
                    continue
                if self._elapsed(current) < self._threshold:
                    continue
                promoted.append(
                    self._settle(current, PROMOTED, _PROMOTED_REASON, pending_notices)
                )
        self._dispatch_notices(pending_notices)
        return tuple(promoted)

    def converge(
        self, decision_id: str, *, accepted: bool, reason: str = ""
    ) -> PendingDecision:
        """Record the async outcome of a pending or promoted decision."""

        pending_notices: list[
            tuple[ConvergenceNotice, tuple[Callable[[ConvergenceNotice], None], ...]]
        ] = []
        with self._lock:
            decision = self._require(decision_id)
            if decision.status in _TERMINAL:
                return decision
            status = CONVERGED if accepted else REJECTED
            updated = self._settle(decision, status, str(reason or ""), pending_notices)
        self._dispatch_notices(pending_notices)
        return updated

    def notices(self) -> tuple[ConvergenceNotice, ...]:
        """Drain the retained convergence notices (oldest first)."""

        with self._lock:
            drained = tuple(self._notices)
            self._notices.clear()
            return drained

    def subscribe(self, callback: Callable[[ConvergenceNotice], None]) -> None:
        """Register a notice callback (best-effort; failures are logged)."""

        if callable(callback):
            with self._lock:
                if callback not in self._callbacks:
                    self._callbacks.append(callback)

    def _require(self, decision_id: str) -> PendingDecision:
        key = str(decision_id or "").strip()
        decision = self._decisions.get(key)
        if decision is None:
            raise KeyError(f"unknown pending decision {key!r}")
        return decision

    def _elapsed(self, decision: PendingDecision) -> float:
        return round(max(0.0, float(self._now()) - decision.created_at), 6)

    def _settle(
        self,
        decision: PendingDecision,
        status: str,
        reason: str,
        pending_notices: list[
            tuple[ConvergenceNotice, tuple[Callable[[ConvergenceNotice], None], ...]]
        ],
    ) -> PendingDecision:
        updated = replace(decision, status=status, reason=reason)
        self._decisions[updated.decision_id] = updated
        notice = ConvergenceNotice(
            decision_id=updated.decision_id,
            status=status,
            elapsed_seconds=self._elapsed(updated),
            reason=reason,
        )
        self._notices.append(notice)
        pending_notices.append((notice, tuple(self._callbacks)))
        return updated

    def _dispatch_notices(
        self,
        pending_notices: list[
            tuple[ConvergenceNotice, tuple[Callable[[ConvergenceNotice], None], ...]]
        ],
    ) -> None:
        for notice, callbacks in pending_notices:
            for callback in callbacks:
                try:
                    callback(notice)
                except Exception:  # best-effort: a subscriber must never break promotion
                    _LOGGER.debug("sync-async notice subscriber failed", exc_info=True)

    def _make_room(
        self,
        pending_notices: list[
            tuple[ConvergenceNotice, tuple[Callable[[ConvergenceNotice], None], ...]]
        ],
    ) -> None:
        if len(self._decisions) < self._max_pending:
            return
        terminal = sorted(
            (
                decision
                for decision in self._decisions.values()
                if decision.status in _TERMINAL
            ),
            key=lambda decision: (decision.created_at, decision.decision_id),
        )
        if terminal:
            self._decisions.pop(terminal[0].decision_id, None)
            return
        oldest = min(
            self._decisions.values(),
            key=lambda decision: (decision.created_at, decision.decision_id),
        )
        self._decisions.pop(oldest.decision_id, None)
        notice = ConvergenceNotice(
            decision_id=oldest.decision_id,
            status=REJECTED,
            elapsed_seconds=self._elapsed(oldest),
            reason=_CAPACITY_REASON,
        )
        self._notices.append(notice)
        pending_notices.append((notice, tuple(self._callbacks)))
