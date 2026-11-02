"""Attention — the Python projection of the gateway's attention surface.

``crates/uc-types/src/governance/attention.rs`` is canonical. This package
restates its two invariants in Python so the dashboard can display them without
re-deriving the rules:

* an attention state never holds a worker slot — a case parked on CI costs no
  execution capacity, and
* only a pending ``needs_human`` decision reserves human-priority capacity.

Nothing here owns state: the Rust gateway does. :mod:`model` holds the pure
projections (kind classification, item projection, worklist order, capacity
arithmetic) and :mod:`capacity` holds the human-reserve policy used by the
standalone dashboard and by the HTTP surface in
:mod:`ultimate_coders.dashboard.attention_api`.
"""

from ultimate_coders.attention.capacity import (
    DEFAULT_HUMAN_RESERVE,
    HUMAN_RESERVE_ENV,
    CapacityTracker,
    human_reserve_from_env,
    slots_from_registry,
)
from ultimate_coders.attention.model import (
    EPOCH,
    MAX_PRIORITY,
    AttentionItem,
    AttentionKind,
    Capacity,
    as_slot_count,
    open_items,
    worklist_order,
)

__all__ = [
    "DEFAULT_HUMAN_RESERVE",
    "EPOCH",
    "HUMAN_RESERVE_ENV",
    "MAX_PRIORITY",
    "AttentionItem",
    "AttentionKind",
    "Capacity",
    "CapacityTracker",
    "as_slot_count",
    "human_reserve_from_env",
    "open_items",
    "slots_from_registry",
    "worklist_order",
]
