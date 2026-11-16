"""Acceptance and verification primitives for the v10 architecture upgrade.

The modules in this package are opt-in and safe to import without side
effects.  They complement the existing oracle/review paths rather than
replacing the authoritative acceptance service.
"""

from __future__ import annotations

__all__ = [
    "anti_pattern_gate",
    "dockerless_verifier",
    "exec_critic",
    "gdpo_scorer",
    "mutation_augment",
    "retrace_verifier",
    "swe_doctor_diag",
]
