"""Attention — the typed "why is this case not progressing" surface.

Two invariants are encoded here, both mirrored from the canonical Rust
definition in ``crates/uc-types/src/governance/attention.rs``:

1. **An attention state never holds a worker slot.** Every
   :class:`AttentionKind` is classified as ``False`` in the module-level
   tables, so a case parked on CI costs no execution capacity. This is the
   ``少打扰`` criterion "waiting for CI must not consume a worker slot",
   expressed as a total function instead of a per-call-site convention: the
   tables are keyed by every kind, so adding a kind without classifying it
   raises ``KeyError`` instead of silently defaulting to "holds a slot".
2. **Only a pending human decision reserves human-priority capacity.**
   :meth:`AttentionKind.reserves_human_slot` is ``True`` for ``needs_human``
   alone. A ``waiting_event`` case reserves nothing, because reserving
   capacity for a case that may be parked for hours is exactly the waste the
   plan forbids.

The worklist order is a deterministic total order (human decisions first,
then priority descending, then oldest ``since``, then ``case_id``) so the
dashboard does not reshuffle between refreshes.

Honest limitations:

* The gateway (Rust) owns attention state. This module only *projects* it; it
  never raises, resolves or persists an item on its own.
* ``since`` arrives as an RFC 3339 string. A timestamp this module cannot
  parse is coerced to the Unix epoch instead of dropping the item, so one
  malformed record cannot hide the rest of the worklist — but the coerced
  item then sorts as the oldest one, which is visible in the ordering.
* :meth:`AttentionItem.to_dict` emits exactly the dashboard wire contract.
  A field the gateway adds later is dropped here until the contract is
  extended; that is deliberate so the SPA can never depend on
  gateway-internal fields.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

#: Coerced timestamp for an unparseable / missing ``since``.
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Highest priority a case may carry (mirrors the Rust ``u8``).
MAX_PRIORITY = 255

# RFC 3339 as chrono serializes it: optional fractional seconds of arbitrary
# precision (Rust emits nanoseconds) and an optional ``Z`` / ``±HH:MM`` offset.
_RFC3339 = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?"
    r"(?P<offset>[Zz]|[+-]\d{2}:?\d{2})?$"
)


class AttentionKind(str, Enum):
    """Why a case cannot move forward without something external happening."""

    WAITING_EVENT = "waiting_event"
    NEEDS_HUMAN = "needs_human"
    BLOCKED = "blocked"
    VERIFYING = "verifying"

    def __str__(self) -> str:
        """Render the wire token, matching the Rust ``Display`` impl."""
        return self.value

    def as_str(self) -> str:
        """Return the wire token (``waiting_event``, ...)."""
        return self.value

    @classmethod
    def from_str_lossy(cls, value: Any) -> AttentionKind | None:
        """Parse a wire token, tolerating surrounding whitespace and case."""
        if isinstance(value, AttentionKind):
            return value
        if not isinstance(value, str):
            return None
        try:
            return cls(value.strip().lower())
        except ValueError:
            return None

    def holds_worker_slot(self) -> bool:
        """Does a case in this state occupy a worker execution slot?

        ``False`` for every kind: an attention state means the case is *not*
        executing. If it were executing the case would be ``active``, which is
        not an attention state at all.
        """
        return _HOLDS_WORKER_SLOT[self]

    def reserves_human_slot(self) -> bool:
        """Does this state reserve human-priority capacity for a decision?

        Only ``needs_human`` does. ``waiting_event`` explicitly does not: the
        reservation exists so a decision can start immediately, and there is
        no decision pending for a case parked on CI.
        """
        return _RESERVES_HUMAN_SLOT[self]

    def requires_human_now(self) -> bool:
        """Does a case in this state interrupt a human?

        Only ``needs_human``. A ``blocked`` case is reported in the dashboard
        but pages nobody: by definition the operator cannot unblock it by
        deciding something right now.
        """
        return _REQUIRES_HUMAN_NOW[self]

    def is_visible_in_worklist(self) -> bool:
        """Should this state be surfaced in the operator worklist?"""
        return _VISIBLE_IN_WORKLIST[self]


# Total tables: one entry per kind, no default. A new kind without a
# classification is a KeyError, never a silent "holds a slot".
_HOLDS_WORKER_SLOT: Mapping[AttentionKind, bool] = {
    AttentionKind.WAITING_EVENT: False,
    AttentionKind.NEEDS_HUMAN: False,
    AttentionKind.BLOCKED: False,
    AttentionKind.VERIFYING: False,
}

_RESERVES_HUMAN_SLOT: Mapping[AttentionKind, bool] = {
    AttentionKind.WAITING_EVENT: False,
    AttentionKind.NEEDS_HUMAN: True,
    AttentionKind.BLOCKED: False,
    AttentionKind.VERIFYING: False,
}

_REQUIRES_HUMAN_NOW: Mapping[AttentionKind, bool] = {
    AttentionKind.WAITING_EVENT: False,
    AttentionKind.NEEDS_HUMAN: True,
    AttentionKind.BLOCKED: False,
    AttentionKind.VERIFYING: False,
}

_VISIBLE_IN_WORKLIST: Mapping[AttentionKind, bool] = {
    AttentionKind.WAITING_EVENT: True,
    AttentionKind.NEEDS_HUMAN: True,
    AttentionKind.BLOCKED: True,
    AttentionKind.VERIFYING: False,
}


def as_slot_count(value: Any) -> int:
    """Coerce a wire counter to a non-negative integer (junk becomes ``0``)."""
    if isinstance(value, bool):
        return int(value)
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _as_utc(value: datetime) -> datetime:
    """Normalise a datetime to aware UTC (naive values are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    """Render a datetime as RFC 3339 with a ``Z`` suffix for UTC."""
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _epoch_millis(value: datetime) -> int:
    """Age key for ordering; millisecond resolution, timezone-independent."""
    return int(_as_utc(value).timestamp() * 1000)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a wire timestamp into aware UTC, or return ``None`` if unparseable."""
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # The NATS bridge sends RFC 3339 strings (chrono serde); a numeric
        # value is accepted as Unix seconds so a proto-shaped int64 still
        # orders correctly instead of silently landing on the epoch.
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    match = _RFC3339.match(value.strip())
    if match is None:
        return None
    base = match.group("base")
    fraction = match.group("fraction") or ""
    if fraction:
        # Python 3.9 only parses exactly 3 or 6 fractional digits; Rust emits
        # nanoseconds, so pad/truncate to microseconds before parsing.
        digits = (fraction[1:] + "000000")[:6]
        fraction = "." + digits
    offset = match.group("offset") or "Z"
    if offset in ("Z", "z"):
        offset = "+00:00"
    elif ":" not in offset:
        offset = offset[:-2] + ":" + offset[-2:]
    try:
        return datetime.fromisoformat(base + fraction + offset).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _coerce_priority(value: Any) -> int:
    """Clamp a wire priority into the Rust ``u8`` range."""
    if isinstance(value, bool):
        return int(value)
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(priority, MAX_PRIORITY))


def _optional_text(value: Any) -> str | None:
    """Return the value as text, or ``None`` when it is absent."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _required_text(value: Any, field: str) -> str:
    """Return a required identity field, or raise ``ValueError`` when absent."""
    text = _optional_text(value)
    if text is None or not text.strip():
        raise ValueError(f"attention item is missing {field!r}")
    return text


@dataclass
class AttentionItem:
    """One thing a case is waiting on (a projection of the gateway record)."""

    id: str
    case_id: str
    kind: AttentionKind
    summary: str
    since: datetime
    task_id: str | None = None
    detail: str | None = None
    priority: int = 0
    human_action: str | None = None
    resolved_at: datetime | None = None
    resolution_summary: str | None = None

    def __post_init__(self) -> None:
        """Normalise the loose inputs a wire projection may carry."""
        kind = AttentionKind.from_str_lossy(self.kind)
        if kind is None:
            raise ValueError(f"unknown attention kind: {self.kind!r}")
        self.kind = kind
        self.since = _as_utc(self.since)
        if self.resolved_at is not None:
            self.resolved_at = _as_utc(self.resolved_at)
        self.priority = _coerce_priority(self.priority)

    def is_open(self) -> bool:
        """True until the item is resolved (mirrors the Rust ``is_open``)."""
        return self.resolved_at is None

    def holds_worker_slot(self) -> bool:
        """A case in an attention state is not executing: no slot is held."""
        return self.kind.holds_worker_slot()

    def reserves_human_slot(self) -> bool:
        """Does this item reserve human-priority capacity?"""
        return self.kind.reserves_human_slot()

    def requires_human_now(self) -> bool:
        """Does this item interrupt a human right now?"""
        return self.kind.requires_human_now()

    def resolve(self, resolution_summary: str, now: datetime | None = None) -> None:
        """Resolve once; a second resolve is a no-op.

        Idempotent on purpose, matching the gateway: re-delivered decisions and
        double clicks must not rewrite the recorded resolution.
        """
        if self.resolved_at is not None:
            return
        self.resolved_at = _as_utc(now) if now is not None else datetime.now(timezone.utc)
        self.resolution_summary = resolution_summary

    def worklist_order_key(self) -> tuple[int, int, int, str, str]:
        """Deterministic total-order key for the operator worklist.

        Human decisions first, then priority descending, then oldest ``since``
        first, then ``case_id``. ``id`` is a final tiebreaker so two items of
        the same case still order deterministically (Rust's key stops at
        ``case_id`` and relies on a stable sort).
        """
        human_first = 0 if self.kind.requires_human_now() else 1
        return (human_first, -self.priority, _epoch_millis(self.since), self.case_id, self.id)

    def to_dict(self) -> dict[str, Any]:
        """Project to the dashboard wire contract (absent optionals omitted)."""
        data: dict[str, Any] = {
            "id": self.id,
            "case_id": self.case_id,
            "kind": self.kind.value,
            "summary": self.summary,
            "since": _isoformat(self.since),
            "priority": self.priority,
        }
        if self.task_id is not None:
            data["task_id"] = self.task_id
        if self.detail is not None:
            data["detail"] = self.detail
        if self.human_action is not None:
            data["human_action"] = self.human_action
        if self.resolved_at is not None:
            data["resolved_at"] = _isoformat(self.resolved_at)
        if self.resolution_summary is not None:
            data["resolution_summary"] = self.resolution_summary
        return data

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AttentionItem:
        """Project one wire record.

        Raises ``ValueError`` when an identity field (``id``, ``case_id``,
        ``kind``) is missing or unusable: a worklist entry without an identity
        cannot be resolved, so it is better rejected at the edge than shown.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("attention item must be a mapping")
        attention_id = _required_text(payload.get("id"), "id")
        case_id = _required_text(payload.get("case_id"), "case_id")
        kind = AttentionKind.from_str_lossy(payload.get("kind"))
        if kind is None:
            raise ValueError(f"unknown attention kind: {payload.get('kind')!r}")
        since = _parse_timestamp(payload.get("since"))
        return cls(
            id=attention_id,
            case_id=case_id,
            kind=kind,
            summary=_optional_text(payload.get("summary")) or "",
            since=since if since is not None else EPOCH,
            task_id=_optional_text(payload.get("task_id")),
            detail=_optional_text(payload.get("detail")),
            priority=_coerce_priority(payload.get("priority")),
            human_action=_optional_text(payload.get("human_action")),
            resolved_at=_parse_timestamp(payload.get("resolved_at")),
            resolution_summary=_optional_text(payload.get("resolution_summary")),
        )


def worklist_order(items: Iterable[AttentionItem]) -> list[AttentionItem]:
    """Return a new list in operator-worklist order (the input is not mutated)."""
    return sorted(items, key=lambda item: item.worklist_order_key())


def open_items(items: Iterable[AttentionItem]) -> list[AttentionItem]:
    """Return only the unresolved items, preserving the given order."""
    return [item for item in items if item.is_open()]


@dataclass(frozen=True)
class Capacity:
    """Capacity accounting: what is reserved for humans and what waiting saved.

    Every counter is coerced to a non-negative integer on construction, so a
    malformed wire value cannot make the derived numbers negative either.
    """

    worker_slots_total: int = 0
    worker_slots_busy: int = 0
    worker_slots_reserved_human: int = 0
    waiting_event_cases: int = 0
    verifying_cases: int = 0
    needs_human_items: int = 0
    blocked_cases: int = 0

    def __post_init__(self) -> None:
        """Clamp every counter at zero (a frozen dataclass needs setattr)."""
        for name in (
            "worker_slots_total",
            "worker_slots_busy",
            "worker_slots_reserved_human",
            "waiting_event_cases",
            "verifying_cases",
            "needs_human_items",
            "blocked_cases",
        ):
            object.__setattr__(self, name, as_slot_count(getattr(self, name)))

    def worker_slots_available(self) -> int:
        """Slots that may accept ordinary work. Never negative.

        Over-subscription (busy + reserved > total) reports zero headroom; a
        negative slot count is not a meaningful answer for a scheduler or a UI.
        """
        return max(
            0,
            self.worker_slots_total
            - self.worker_slots_busy
            - self.worker_slots_reserved_human,
        )

    def can_admit_worker(self) -> bool:
        """Would admitting one more ordinary execution fit reserved capacity?"""
        return self.worker_slots_available() > 0

    def slots_saved_by_quiet_waiting(self) -> int:
        """Slots waiting/verifying cases would have cost had they held slots.

        This is the ``少打扰`` metric made observable: it equals
        ``waiting_event_cases + verifying_cases`` by construction.
        """
        return self.waiting_event_cases + self.verifying_cases

    def to_dict(self) -> dict[str, int]:
        """Project to the dashboard wire contract, derived fields included."""
        return {
            "worker_slots_total": self.worker_slots_total,
            "worker_slots_busy": self.worker_slots_busy,
            "worker_slots_reserved_human": self.worker_slots_reserved_human,
            "waiting_event_cases": self.waiting_event_cases,
            "verifying_cases": self.verifying_cases,
            "needs_human_items": self.needs_human_items,
            "blocked_cases": self.blocked_cases,
            "worker_slots_available": self.worker_slots_available(),
            "slots_saved_by_quiet_waiting": self.slots_saved_by_quiet_waiting(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> Capacity:
        """Read the counters from a gateway reply.

        The derived fields (``worker_slots_available``,
        ``slots_saved_by_quiet_waiting``) are always recomputed, never copied:
        a stale or partial reply must not be able to make the two disagree
        with the counters it shipped.
        """
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            worker_slots_total=as_slot_count(payload.get("worker_slots_total")),
            worker_slots_busy=as_slot_count(payload.get("worker_slots_busy")),
            worker_slots_reserved_human=as_slot_count(
                payload.get("worker_slots_reserved_human")
            ),
            waiting_event_cases=as_slot_count(payload.get("waiting_event_cases")),
            verifying_cases=as_slot_count(payload.get("verifying_cases")),
            needs_human_items=as_slot_count(payload.get("needs_human_items")),
            blocked_cases=as_slot_count(payload.get("blocked_cases")),
        )


__all__ = [
    "EPOCH",
    "MAX_PRIORITY",
    "AttentionItem",
    "AttentionKind",
    "Capacity",
    "as_slot_count",
    "open_items",
    "worklist_order",
]
