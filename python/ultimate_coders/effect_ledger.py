"""Durable effect ledger: one logical external operation, many safe attempts.

An *effect* is something that changes the world outside the process -- a
branch push, a merge published to a remote, an outbound notification. It is
not an attempt: a subtask may retry five times, but the logical publish it
performs is one effect, and its retries must share one identity so that
"did this already happen?" has an answer that does not depend on the model
guessing.

The ledger exists for one failure mode above all others: **an operation that
succeeded but whose response was lost**. Treating that as FAILED is the
expensive mistake -- the caller retries and the side effect happens twice.
The state machine therefore separates:

- ``FAILED``: we know it did not happen (e.g. the remote rejected it).
- ``UNKNOWN``: we cannot tell (connection dropped mid-request).
- ``RECONCILING``: someone is actively asking the external system.

Only ``FAILED`` may be retried directly. ``UNKNOWN`` must be reconciled
first: query the external system, and only a definite "it did not happen"
returns the effect to ``PREPARED``. ``CONFIRMED`` is terminal -- a repeat
call under the same effect id returns the recorded result and the caller
must not execute again.

Storage is the existing :class:`~ultimate_coders.runtime_state.RuntimeState`
(versioned, transactional records): transitions run inside ``mutate``, so
the check-and-set is atomic and survives process restarts. The ledger adds
no new storage engine and no distributed lock; ``owner`` + ``lease_until``
is a soft lease for crash detection, not a mutual-exclusion primitive.
"""

from __future__ import annotations

import hashlib
import time
from enum import Enum
from typing import Any

from .runtime_state import RuntimeState

#: Namespace for effect records. ``status`` is what RuntimeState indexes, so
#: ``state.query("tool_effects", status="UNKNOWN")`` lists what needs a
#: reconciler -- no separate index to keep in sync.
NAMESPACE = "tool_effects"


class EffectStatus(str, Enum):
    PREPARED = "PREPARED"  # intent recorded, nothing sent yet
    IN_FLIGHT = "IN_FLIGHT"  # a worker holds the lease and is executing
    CONFIRMED = "CONFIRMED"  # the external system accepted it -- terminal
    FAILED = "FAILED"  # definitely did not happen -- retry is safe
    UNKNOWN = "UNKNOWN"  # may or may not have happened -- reconcile first
    RECONCILING = "RECONCILING"  # a worker is querying the external system


#: Allowed transitions. Anything else raises: a silent illegal move would
#: turn an ambiguous outcome into a confident wrong one.
TRANSITIONS: dict[EffectStatus, frozenset[EffectStatus]] = {
    EffectStatus.PREPARED: frozenset(
        {EffectStatus.IN_FLIGHT, EffectStatus.FAILED, EffectStatus.UNKNOWN}
    ),
    EffectStatus.IN_FLIGHT: frozenset(
        {EffectStatus.CONFIRMED, EffectStatus.FAILED, EffectStatus.UNKNOWN}
    ),
    EffectStatus.UNKNOWN: frozenset(
        {EffectStatus.RECONCILING, EffectStatus.CONFIRMED, EffectStatus.PREPARED}
    ),
    EffectStatus.RECONCILING: frozenset(
        {EffectStatus.CONFIRMED, EffectStatus.PREPARED, EffectStatus.UNKNOWN}
    ),
    EffectStatus.FAILED: frozenset({EffectStatus.PREPARED}),  # explicit retry
    EffectStatus.CONFIRMED: frozenset(),  # terminal
}


class EffectTransitionError(RuntimeError):
    """An illegal state change was attempted (bug, not an external failure)."""


class EffectBusyError(RuntimeError):
    """Another worker holds a live lease on this effect."""


def stable_effect_id(kind: str, *parts: object) -> str:
    """Deterministic id for one logical operation.

    Callers pass what makes the operation *the same operation* across
    retries -- for a publish, the remote, the branch and the commit being
    published -- never the attempt number or wall-clock time.
    """
    material = "\x1f".join([kind, *(str(p) for p in parts)])
    return "eff_" + hashlib.sha1(material.encode("utf-8")).hexdigest()[:24]


class EffectLedger:
    """Effect records on top of a RuntimeState store."""

    def __init__(self, state: RuntimeState, *, namespace: str = NAMESPACE) -> None:
        self.state = state
        self.namespace = namespace

    # ── reads ───────────────────────────────────────────────────

    def get(self, effect_id: str) -> dict[str, Any] | None:
        return self.state.get(self.namespace, effect_id)

    def unknown(self, limit: int = 100) -> list[dict[str, Any]]:
        """Effects whose outcome needs reconciliation (oldest first)."""
        return self.state.query(self.namespace, status=EffectStatus.UNKNOWN.value, limit=limit)

    def in_flight(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.state.query(self.namespace, status=EffectStatus.IN_FLIGHT.value, limit=limit)

    def by_prefix(self, prefix: str, limit: int = 100) -> list[dict[str, Any]]:
        """Records whose effect id starts with ``prefix`` (task-scoped views)."""
        return [
            r
            for r in self.state.records(self.namespace)
            if str(r.get("effect_id", "")).startswith(prefix)
        ][:limit]

    # ── transitions ─────────────────────────────────────────────

    def prepare(
        self,
        effect_id: str,
        *,
        kind: str,
        request: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_before: str | None = None,
        expected_after: str | None = None,
    ) -> dict[str, Any]:
        """Record the intent (or return the existing record for this id).

        A live lease raises :class:`EffectBusyError`; an expired one means
        the owner died mid-flight, so the effect becomes ``UNKNOWN`` -- the
        caller must reconcile rather than assume it failed.
        """
        now = time.time()

        def change(old: dict[str, Any]) -> dict[str, Any]:
            if not old:
                return {
                    "effect_id": effect_id,
                    "kind": kind,
                    "status": EffectStatus.PREPARED.value,
                    "request": request or {},
                    "idempotency_key": idempotency_key or effect_id,
                    "expected_before": expected_before,
                    "expected_after": expected_after,
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                }
            status = EffectStatus(old.get("status", ""))
            if status is EffectStatus.CONFIRMED:
                return old
            if status is EffectStatus.IN_FLIGHT:
                if float(old.get("lease_until", 0)) > now:
                    raise EffectBusyError(
                        f"{effect_id} is held by {old.get('owner', '?')} "
                        f"until {old.get('lease_until')}"
                    )
                return _moved(old, EffectStatus.UNKNOWN, now, reason="lease expired")
            if status in (EffectStatus.UNKNOWN, EffectStatus.RECONCILING):
                return old  # caller must reconcile before executing again
            if status is EffectStatus.FAILED:
                return _moved(
                    old, EffectStatus.PREPARED, now, reason="retry after definite failure"
                )
            return old  # already PREPARED

        return self.state.mutate(self.namespace, effect_id, change)

    def mark_in_flight(
        self, effect_id: str, *, owner: str, lease_seconds: float = 120
    ) -> dict[str, Any]:
        now = time.time()

        def change(old: dict[str, Any]) -> dict[str, Any]:
            _require(old, effect_id, EffectStatus.PREPARED)
            return _moved(
                old,
                EffectStatus.IN_FLIGHT,
                now,
                owner=owner,
                lease_until=now + lease_seconds,
                attempts=int(old.get("attempts", 0)) + 1,
            )

        return self.state.mutate(self.namespace, effect_id, change)

    def confirm(self, effect_id: str, *, result: dict[str, Any] | None = None) -> dict[str, Any]:
        """Mark the effect as applied. Legal from any non-terminal state."""
        now = time.time()

        def change(old: dict[str, Any]) -> dict[str, Any]:
            _require(old, effect_id)
            status = EffectStatus(old.get("status", ""))
            if status is EffectStatus.CONFIRMED:
                return old
            if status not in (
                EffectStatus.IN_FLIGHT,
                EffectStatus.UNKNOWN,
                EffectStatus.RECONCILING,
            ):
                raise EffectTransitionError(f"{effect_id}: cannot confirm from {status.value}")
            return _moved(
                old,
                EffectStatus.CONFIRMED,
                now,
                owner="",
                lease_until=0,
                result=result or {},
                confirmed_at=now,
            )

        return self.state.mutate(self.namespace, effect_id, change)

    def fail(
        self, effect_id: str, *, error: str, result: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Record a *definite* non-execution (e.g. the remote rejected it)."""
        now = time.time()

        def change(old: dict[str, Any]) -> dict[str, Any]:
            _require(old, effect_id)
            return _moved(
                old,
                EffectStatus.FAILED,
                now,
                owner="",
                lease_until=0,
                error=error,
                result=result or {},
            )

        return self.state.mutate(self.namespace, effect_id, change)

    def mark_unknown(self, effect_id: str, *, reason: str) -> dict[str, Any]:
        """Record an ambiguous outcome: it may or may not have happened."""
        now = time.time()

        def change(old: dict[str, Any]) -> dict[str, Any]:
            _require(old, effect_id)
            return _moved(
                old,
                EffectStatus.UNKNOWN,
                now,
                owner="",
                lease_until=0,
                reason=reason,
                unknown_at=now,
            )

        return self.state.mutate(self.namespace, effect_id, change)

    def begin_reconcile(self, effect_id: str, *, owner: str) -> dict[str, Any]:
        now = time.time()

        def change(old: dict[str, Any]) -> dict[str, Any]:
            _require(old, effect_id, EffectStatus.UNKNOWN)
            return _moved(old, EffectStatus.RECONCILING, now, owner=owner, reason="")

        return self.state.mutate(self.namespace, effect_id, change)

    def resolve_reconcile(
        self,
        effect_id: str,
        *,
        applied: bool,
        result: dict[str, Any] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Settle a reconciliation.

        ``applied=True`` -> CONFIRMED (stop; do not execute again).
        ``applied=False`` -> PREPARED (the external system definitely does
        not have it, so retrying is safe).
        """
        now = time.time()

        def change(old: dict[str, Any]) -> dict[str, Any]:
            _require(old, effect_id, EffectStatus.RECONCILING)
            if applied:
                return _moved(
                    old,
                    EffectStatus.CONFIRMED,
                    now,
                    owner="",
                    result=result or {},
                    reason=reason,
                    confirmed_at=now,
                )
            return _moved(old, EffectStatus.PREPARED, now, owner="", reason=reason)

        return self.state.mutate(self.namespace, effect_id, change)


# ── helpers ─────────────────────────────────────────────────────


def _require(old: dict[str, Any], effect_id: str, *allowed: EffectStatus) -> None:
    if not old:
        raise EffectTransitionError(f"{effect_id}: no effect record")
    status = EffectStatus(old.get("status", ""))
    if allowed and status not in allowed:
        expected = "/".join(s.value for s in allowed)
        raise EffectTransitionError(f"{effect_id}: {status.value} is not {expected}")


def _moved(old: dict[str, Any], to: EffectStatus, now: float, **fields: Any) -> dict[str, Any]:
    current = EffectStatus(old.get("status", ""))
    if to not in TRANSITIONS.get(current, frozenset()):
        raise EffectTransitionError(f"{old.get('effect_id')}: {current.value} -> {to.value}")
    return {
        **old,
        **fields,
        "status": to.value,
        "updated_at": now,
        "last_transition_at": now,
    }
