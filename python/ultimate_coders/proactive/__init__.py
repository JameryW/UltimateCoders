"""Proactive autonomy, bounded: evaluate facts, propose with receipts, never decide.

This package is the Python half of the proactive plane. It exists to answer two
questions and nothing else:

* **what is worth doing?** -- :mod:`ultimate_coders.proactive.evaluator` turns a
  structured context (case/task state, the last CI outcome, the progress
  detector's stall verdict, budget usage) into zero or more proposals. Every
  proposal carries stable evidence ids; a candidate without evidence is refused
  rather than emitted.
* **what should a stalled task do?** -- :mod:`ultimate_coders.proactive.watchdog_input`
  builds the deterministic ``watchdog.stall.report`` payload from the existing
  progress detector and maps the gateway's bounded verdict back to one
  orchestrator action, refusing a replan that does not preserve confirmed
  external effects.

The boundary is the point: the Rust control plane owns admission, budget, state
and execution authorization. Nothing in this package admits a proposal, spends a
token, persists a decision or executes work. :mod:`ultimate_coders.proactive.modes`
only parses the operator's autonomy ceiling, and it fails closed (unknown value
-> ``off`` with a warning).

Honest limitations:

* Rules are intentionally few and auditable; richer judgement is not implemented
  rather than guessed.
* The HTTP surface (``ultimate_coders.dashboard.proactive_api``) is not imported
  here on purpose: the core package must not require FastAPI, and the API is a
  transport for the gateway, not a second policy engine.
* The evaluator's confidence is a review-ordering hint. It never grants
  execution, and no threshold on it exists anywhere in this package.
"""

from __future__ import annotations

from ultimate_coders.proactive.evaluator import (
    BASE_CONFIDENCE,
    ESTIMATED_TOKENS,
    MAX_PROPOSALS_PER_RULE,
    PROPOSAL_KIND_TOKENS,
    EvaluationResult,
    Judge,
    ProactiveEvaluator,
    Proposal,
    ProposalKind,
    Refusal,
)
from ultimate_coders.proactive.modes import (
    DEFAULT_PROACTIVE_MODE,
    ENV_PROACTIVE_MODE,
    PROACTIVE_MODES,
    ProactiveMode,
    parse_proactive_mode,
    proactive_mode_from_env,
)
from ultimate_coders.proactive.watchdog_input import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_REPLANS,
    DEFAULT_STALL_LIMIT,
    ENV_PROGRESS_STALL_LIMIT,
    ENV_WATCHDOG_MAX_ATTEMPTS,
    ENV_WATCHDOG_MAX_REPLANS,
    VERDICT_ACTIONS,
    WATCHDOG_ACTIONS,
    WatchdogAction,
    WatchdogContractError,
    WatchdogLimits,
    apply_verdict,
    build_stall_signal,
    normalize_stall_report,
    progress_stall_limit,
)

__all__ = [
    # modes
    "DEFAULT_PROACTIVE_MODE",
    "ENV_PROACTIVE_MODE",
    "PROACTIVE_MODES",
    "ProactiveMode",
    "parse_proactive_mode",
    "proactive_mode_from_env",
    # evaluator
    "BASE_CONFIDENCE",
    "ESTIMATED_TOKENS",
    "MAX_PROPOSALS_PER_RULE",
    "PROPOSAL_KIND_TOKENS",
    "EvaluationResult",
    "Judge",
    "Proposal",
    "ProposalKind",
    "ProactiveEvaluator",
    "Refusal",
    # watchdog input/output
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_REPLANS",
    "DEFAULT_STALL_LIMIT",
    "ENV_PROGRESS_STALL_LIMIT",
    "ENV_WATCHDOG_MAX_ATTEMPTS",
    "ENV_WATCHDOG_MAX_REPLANS",
    "VERDICT_ACTIONS",
    "WATCHDOG_ACTIONS",
    "WatchdogAction",
    "WatchdogContractError",
    "WatchdogLimits",
    "apply_verdict",
    "build_stall_signal",
    "normalize_stall_report",
    "progress_stall_limit",
]
