"""SetupX LIFO experience replay (#69), Python side.

The Rust ``setupx_lifo`` module owns the snapshot contract; this module is the
portable experience store used by the Python worker. It keeps a bounded set of
experience units (signature, guidance, repair actions), a Beta-smoothed
posterior per unit, and a LIFO copy-on-write snapshot stack. The prosecutor and
judge stay separate in the caller: this store only accepts the already-observed
evidence bundle and applies the rollback/retention rule.

Enable with ``UC_XPU_STORE=1``. No Docker, filesystem or model access.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "SetupEvidence",
    "SetupXError",
    "SetupXRunner",
    "Snapshot",
    "Verdict",
    "Xpu",
    "XpuStore",
    "xpu_store_enabled",
]

XPU_STORE_ENV = "UC_XPU_STORE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def xpu_store_enabled() -> bool:
    return os.environ.get(XPU_STORE_ENV, "").strip().lower() in _TRUTHY


def _non_empty(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


class SetupXError(ValueError):
    """Raised for invalid SetupX bookkeeping."""


@dataclass(frozen=True)
class Xpu:
    """One experience unit."""

    id: str
    signature: str
    guidance: str
    actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.id, "id")
        _non_empty(self.signature, "signature")


@dataclass(frozen=True)
class SetupEvidence:
    """Prosecutor evidence for one environment."""

    dynamic_link_ok: bool
    ports_ok: bool
    smoke_ok: bool

    @property
    def all_ok(self) -> bool:
        return self.dynamic_link_ok and self.ports_ok and self.smoke_ok


class Verdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Snapshot:
    """One LIFO snapshot record."""

    id: str
    xpu_id: str
    digest: str
    depth: int


@dataclass(frozen=True)
class SpeculationOutcome:
    xpu_id: str
    snapshot_id: str
    verdict: Verdict
    posterior: float


class XpuStore:
    """Bounded experience store with Beta-smoothed posteriors."""

    def __init__(self, max_xpus: int = 64) -> None:
        if max_xpus <= 0:
            raise ValueError("max_xpus must be positive")
        self._max = max_xpus
        self._xpus: dict[str, Xpu] = {}
        self._outcomes: dict[str, tuple[int, int]] = {}

    def register(self, xpu: Xpu) -> None:
        if xpu.id in self._xpus:
            raise SetupXError(f"xpU {xpu.id!r} is already registered")
        if len(self._xpus) >= self._max:
            raise SetupXError(f"xpU capacity of {self._max} reached")
        self._xpus[xpu.id] = xpu

    def posterior(self, xpu_id: str) -> float | None:
        if xpu_id not in self._xpus:
            return None
        successes, attempts = self._outcomes.get(xpu_id, (0, 0))
        return (successes + 1.0) / (attempts + 2.0)

    def record(self, xpu_id: str, success: bool) -> float:
        if xpu_id not in self._xpus:
            raise SetupXError(f"unknown xpU {xpu_id!r}")
        successes, attempts = self._outcomes.get(xpu_id, (0, 0))
        self._outcomes[xpu_id] = (
            successes + (1 if success else 0),
            attempts + 1,
        )
        posterior = self.posterior(xpu_id)
        assert posterior is not None
        return posterior

    def match(self, signature: str) -> Xpu | None:
        """Best exact-signature XPU; ties prefer the lexicographically first id."""

        candidates = [xpu for xpu in self._xpus.values() if xpu.signature == signature]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda xpu: (-(self.posterior(xpu.id) or 0.5), xpu.id),
        )

    def __len__(self) -> int:
        return len(self._xpus)


class SnapshotStack:
    """Bounded LIFO stack of copy-on-write snapshots."""

    def __init__(self, max_depth: int = 64) -> None:
        if max_depth <= 0:
            raise ValueError("max_depth must be positive")
        self._max = max_depth
        self._items: list[Snapshot] = []

    def push(self, snapshot: Snapshot) -> None:
        if len(self._items) >= self._max:
            raise SetupXError(f"snapshot capacity of {self._max} reached")
        self._items.append(snapshot)

    def pop(self) -> Snapshot:
        if not self._items:
            raise SetupXError("snapshot stack is empty")
        return self._items.pop()

    def peek(self) -> Snapshot | None:
        return self._items[-1] if self._items else None

    def __len__(self) -> int:
        return len(self._items)


class SetupXRunner:
    """Speculate, judge, and roll back environment experience units."""

    def __init__(
        self,
        store: XpuStore | None = None,
        stack: SnapshotStack | None = None,
    ) -> None:
        self.store = store if store is not None else XpuStore()
        self.stack = stack if stack is not None else SnapshotStack()
        self._pending: Snapshot | None = None

    @property
    def pending(self) -> Snapshot | None:
        return self._pending

    def speculate(self, signature: str) -> tuple[Xpu, Snapshot]:
        if self._pending is not None:
            raise SetupXError("a speculation is already pending")
        xpu = self.store.match(signature)
        if xpu is None:
            raise SetupXError(f"no xpU matches signature {signature!r}")
        depth = len(self.stack) + 1
        digest = hashlib.sha256(
            f"{xpu.id}:{xpu.signature}:{depth}".encode()
        ).hexdigest()
        snapshot = Snapshot(
            id=f"snap-{depth}-{xpu.id}",
            xpu_id=xpu.id,
            digest=digest,
            depth=depth,
        )
        self.stack.push(snapshot)
        self._pending = snapshot
        return xpu, snapshot

    def judge(self, evidence: SetupEvidence) -> SpeculationOutcome:
        if self._pending is None:
            raise SetupXError("no speculation is pending")
        snapshot = self._pending
        self._pending = None
        verdict = Verdict.ACCEPTED if evidence.all_ok else Verdict.REJECTED
        if verdict is Verdict.REJECTED:
            popped = self.stack.pop()
            if popped.id != snapshot.id:
                raise SetupXError("snapshot stack is inconsistent")
        posterior = self.store.record(
            snapshot.xpu_id, verdict is Verdict.ACCEPTED
        )
        return SpeculationOutcome(
            xpu_id=snapshot.xpu_id,
            snapshot_id=snapshot.id,
            verdict=verdict,
            posterior=posterior,
        )

    def rollback(self) -> Snapshot:
        if self._pending is not None:
            raise SetupXError("a speculation is pending")
        return self.stack.pop()
