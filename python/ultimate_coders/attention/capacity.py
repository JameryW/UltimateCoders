"""Capacity accounting: the human reserve and the quiet-waiting saving.

The gateway owns the scheduler's real capacity (the ``capacity.get`` op); this
module derives the same shape locally from a worker-registry snapshot plus the
open attention items. It exists so a standalone dashboard process — one that
receives a worker snapshot over NATS but talks to the gateway only for
governance state — can report the same numbers without inventing a second
scheduler, and so the reserve policy is a pure, testable function.

Policy encoded here:

* ``worker_slots_reserved_human`` is **armed only by pending human work**. With
  no open ``needs_human`` item nothing is reserved, even when
  ``UC_ATTENTION_HUMAN_RESERVE`` is set: holding capacity for a decision that
  nobody has asked for yet is the waste ``少打扰`` forbids. Once at least one
  human decision is pending, the reserve is
  ``max(UC_ATTENTION_HUMAN_RESERVE, open needs_human items)`` capped by the
  configured slot total, so every pending decision can start immediately.
* ``worker_slots_available`` is clamped at zero. Over-subscription
  (busy + reserved > total) is reported as zero headroom, never as a negative
  count.
* ``slots_saved_by_quiet_waiting`` counts the slots the parked cases would have
  occupied had they held one, i.e. ``waiting_event_cases + verifying_cases``.

Honest limitations:

* The tracker counts distinct *cases* for the ``*_cases`` counters (a case with
  two open attention items is one parked case) and distinct *items* for
  ``needs_human_items`` (the worklist depth a human actually sees).
* ``worker_slots_busy`` is reported exactly as the registry reports it, even
  when it exceeds ``worker_slots_total``; only the available count is clamped.
  Hiding an over-subscribed registry would hide a real scheduling bug.
* This is a projection, not an authority: it can disagree with the gateway
  between two heartbeats, and the gateway always wins.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from typing import Any

from ultimate_coders.attention.model import (
    AttentionItem,
    AttentionKind,
    Capacity,
    as_slot_count,
    open_items,
)

logger = logging.getLogger(__name__)

#: Environment variable holding the human-slot floor.
HUMAN_RESERVE_ENV = "UC_ATTENTION_HUMAN_RESERVE"

#: Default human-slot floor: one decision can always start immediately.
DEFAULT_HUMAN_RESERVE = 1


def human_reserve_from_env(environ: Mapping[str, str] | None = None) -> int:
    """Read the configured human-slot floor.

    Unset, empty or malformed values fall back to
    :data:`DEFAULT_HUMAN_RESERVE`; a negative value is clamped to ``0``. Never
    fails: a typo in an env var must not take the dashboard down.
    """
    source = os.environ if environ is None else environ
    raw = source.get(HUMAN_RESERVE_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_HUMAN_RESERVE
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using %d",
            HUMAN_RESERVE_ENV,
            raw,
            DEFAULT_HUMAN_RESERVE,
        )
        return DEFAULT_HUMAN_RESERVE
    if value < 0:
        logger.warning("%s=%r is negative; clamping to 0", HUMAN_RESERVE_ENV, raw)
        return 0
    return value


def slots_from_registry(snapshot: Mapping[str, Any] | None) -> tuple[int, int]:
    """Derive ``(total, busy)`` worker slots from a worker-registry snapshot.

    Two shapes are accepted, because two producers exist:

    * an already-aggregated snapshot with ``worker_slots_total`` /
      ``worker_slots_busy`` (what the gateway reports), and
    * the dashboard's per-worker snapshot ``{"workers": [{"max_capacity": n,
      "current_load": m}, ...]}``, summed over the registered workers.

    Missing, malformed or negative values count as zero; the caller gets a
    best-effort pair rather than an exception on a partially-filled snapshot.
    """
    if not isinstance(snapshot, Mapping):
        return (0, 0)
    if "worker_slots_total" in snapshot or "worker_slots_busy" in snapshot:
        return (
            as_slot_count(snapshot.get("worker_slots_total")),
            as_slot_count(snapshot.get("worker_slots_busy")),
        )
    workers = snapshot.get("workers")
    if not isinstance(workers, (list, tuple)):
        return (0, 0)
    total = 0
    busy = 0
    for worker in workers:
        if not isinstance(worker, Mapping):
            continue
        total += as_slot_count(worker.get("max_capacity"))
        busy += as_slot_count(worker.get("current_load"))
    return (total, busy)


class CapacityTracker:
    """Turn a worker-registry snapshot and open attention items into Capacity.

    The tracker holds no state besides the configured reserve floor, so one
    instance can be reused across refreshes and shared by concurrent handlers.
    """

    def __init__(self, human_reserve: int | None = None) -> None:
        """Create a tracker; ``None`` uses the default floor (not the env)."""
        if human_reserve is None:
            reserve = DEFAULT_HUMAN_RESERVE
        else:
            reserve = max(0, int(human_reserve))
        self._human_reserve = reserve

    @property
    def human_reserve(self) -> int:
        """The configured floor for reserved human-priority slots."""
        return self._human_reserve

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CapacityTracker:
        """Build a tracker using ``UC_ATTENTION_HUMAN_RESERVE`` (default ``1``)."""
        return cls(human_reserve=human_reserve_from_env(environ))

    def snapshot(
        self,
        items: Iterable[AttentionItem],
        registry: Mapping[str, Any] | None = None,
    ) -> Capacity:
        """Capacity from a worker-registry snapshot plus the attention items."""
        total, busy = slots_from_registry(registry)
        return self.project(items, worker_slots_total=total, worker_slots_busy=busy)

    def project(
        self,
        items: Iterable[AttentionItem],
        *,
        worker_slots_total: int,
        worker_slots_busy: int,
    ) -> Capacity:
        """Pure projection: counters from the items, reserve from the policy."""
        parked = open_items(items)
        needs_human = sum(1 for item in parked if item.kind is AttentionKind.NEEDS_HUMAN)
        waiting_event_cases = len(
            {item.case_id for item in parked if item.kind is AttentionKind.WAITING_EVENT}
        )
        verifying_cases = len(
            {item.case_id for item in parked if item.kind is AttentionKind.VERIFYING}
        )
        blocked_cases = len(
            {item.case_id for item in parked if item.kind is AttentionKind.BLOCKED}
        )
        total = as_slot_count(worker_slots_total)
        reserved = 0 if needs_human == 0 else min(total, max(self._human_reserve, needs_human))
        return Capacity(
            worker_slots_total=total,
            worker_slots_busy=as_slot_count(worker_slots_busy),
            worker_slots_reserved_human=reserved,
            waiting_event_cases=waiting_event_cases,
            verifying_cases=verifying_cases,
            needs_human_items=needs_human,
            blocked_cases=blocked_cases,
        )


__all__ = [
    "DEFAULT_HUMAN_RESERVE",
    "HUMAN_RESERVE_ENV",
    "CapacityTracker",
    "human_reserve_from_env",
    "slots_from_registry",
]
