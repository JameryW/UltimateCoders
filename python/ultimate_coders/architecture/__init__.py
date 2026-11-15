"""V9 architecture-upgrade components.

Every component in this package is opt-in and safe to import when its
feature flag is unset.  The package groups the six upgrades from the v9
ledger so they can be reviewed, tested and rolled back independently:

* :mod:`~ultimate_coders.architecture.behavior_router` - adapter behavior
  profiles and the complementary retry router.
* :mod:`~ultimate_coders.architecture.mandate` - immutable narrowing
  mandates.
* :mod:`~ultimate_coders.architecture.sync_async_promotion` - five-second
  sync-to-async promotion and Event Gate convergence notices.
* :mod:`~ultimate_coders.architecture.memcredit` - marginal-utility
  retrieval arbitration over text/AST candidates.
* :mod:`~ultimate_coders.architecture.bitemporal` - bitemporal,
  epistemic-status-aware claim graph.
* :mod:`~ultimate_coders.architecture.evolution` - AST-anchored compact
  attempt evolution.
* :mod:`~ultimate_coders.architecture.context_folding` - ACP interval
  folding with verified-read checks.
* :mod:`~ultimate_coders.architecture.process_verifier` - process-level
  acceptance findings for test bypasses.
* :mod:`~ultimate_coders.architecture.credential_broker` - short-lived
  capability leases for sandbox children.
* :mod:`~ultimate_coders.architecture.trajectory_export` - TITO JSONL
  trajectory export for failed/rescued attempt pairs.
"""

from __future__ import annotations

__all__ = [
    "behavior_router",
    "bitemporal",
    "context_folding",
    "credential_broker",
    "evolution",
    "mandate",
    "memcredit",
    "process_verifier",
    "sync_async_promotion",
    "trajectory_export",
]
