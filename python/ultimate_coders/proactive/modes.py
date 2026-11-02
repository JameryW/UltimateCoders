"""Proactive autonomy modes: how much the system may do without asking.

The mode is a ceiling, not a permission. It says how much autonomy the operator
has enabled; admission, budget, state and execution authorization stay with the
Rust control plane (``crates/uc-types/src/governance/proactive.rs``). This module
only parses that ceiling, and it parses it in the safe direction: an unknown or
misspelled ``UC_PROACTIVE_MODE`` falls back to ``off`` with a warning. A typo
must never turn autonomy on -- the operator has to notice a feature that stayed
*disabled*, never one that quietly *enabled* itself.

The four modes mirror the gateway enum exactly:

* ``off`` -- nothing is evaluated (the default);
* ``observe`` -- suggestions are evaluated and recorded, never surfaced as
  actionable and never executed. This is the measurement mode;
* ``propose`` -- suggestions are surfaced for a human decision; approval is
  required before any work starts;
* ``execute`` -- an *approved* suggestion may start work without a further human
  step. Enabling it is an explicit operator decision.

Honest limitations:

* :meth:`ProactiveMode.allows_execution` describes the ceiling only. The gateway
  additionally requires an approved proposal, a principal holding
  ``proactive_execute`` and a token estimate inside ``max_tokens_per_action``;
  ``always_require_approval`` can re-add the human step even in ``execute``.
* The environment is read at the call site (never cached), so a process that
  changes it sees the change. The gateway's persisted mode is the authority;
  this environment value only seeds a fresh gateway.
* Nothing here executes, admits or persists a proposal. A mode that says
  "allowed" is still refused by the gateway when evidence, capacity or budget
  say no.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

#: Environment variable holding the operator-selected ceiling.
ENV_PROACTIVE_MODE = "UC_PROACTIVE_MODE"

#: Default when the variable is unset. Fail closed: autonomy is opt-in.
DEFAULT_PROACTIVE_MODE = "off"


class ProactiveMode(str, Enum):
    """How much autonomy is enabled. Values are the gateway's wire tokens."""

    OFF = "off"
    OBSERVE = "observe"
    PROPOSE = "propose"
    EXECUTE = "execute"

    def __str__(self) -> str:
        return self.value

    def evaluates(self) -> bool:
        """Is any evaluation performed at all?"""
        return self is not ProactiveMode.OFF

    def allows_proposal(self) -> bool:
        """May a proposal be recorded *and* surfaced for a human decision?"""
        return self in (ProactiveMode.PROPOSE, ProactiveMode.EXECUTE)

    def allows_execution(self) -> bool:
        """May an *approved* proposal start work without a further human step?"""
        return self is ProactiveMode.EXECUTE


#: Every canonical token, in increasing order of autonomy (for UIs and tests).
PROACTIVE_MODES: tuple[ProactiveMode, ...] = (
    ProactiveMode.OFF,
    ProactiveMode.OBSERVE,
    ProactiveMode.PROPOSE,
    ProactiveMode.EXECUTE,
)

#: Accepted spellings. ``disabled`` and the empty string mean ``off`` -- the
#: gateway's ``ProactiveMode::from_str_lossy`` accepts the same set, so a value
#: understood by one half of the system is understood by the other.
_MODE_ALIASES: Mapping[str, ProactiveMode] = {
    "": ProactiveMode.OFF,
    "off": ProactiveMode.OFF,
    "disabled": ProactiveMode.OFF,
    "observe": ProactiveMode.OBSERVE,
    "propose": ProactiveMode.PROPOSE,
    "execute": ProactiveMode.EXECUTE,
}


def parse_proactive_mode(raw: Any) -> ProactiveMode | None:
    """Strictly parse one token. ``None`` means "not a mode I understand".

    Accepts a :class:`ProactiveMode` unchanged, and a string with surrounding
    whitespace or different casing. Returns ``None`` for anything else -- the
    caller decides whether that is an environment typo (fall back to ``off``)
    or an operator request to reject (``400`` in the dashboard API). Parsing
    never guesses: coercing an unknown token to a *valid* mode is exactly the
    silent behaviour this module exists to prevent.
    """
    if isinstance(raw, ProactiveMode):
        return raw
    if not isinstance(raw, str):
        return None
    return _MODE_ALIASES.get(raw.strip().lower())


def proactive_mode_from_env(environ: Mapping[str, str] | None = None) -> ProactiveMode:
    """Read ``UC_PROACTIVE_MODE``, defaulting to ``off``.

    An unset or empty variable is the documented default and stays quiet. Any
    other unrecognized value is logged at warning level and treated as ``off``:
    the process keeps running, but autonomy is not granted by accident. The
    return value is always a valid mode, so callers never have to re-check.
    """
    source = os.environ if environ is None else environ
    raw = source.get(ENV_PROACTIVE_MODE, "")
    mode = parse_proactive_mode(raw)
    if mode is not None:
        return mode
    logger.warning(
        "Unknown %s=%r; proactive autonomy stays off (valid: %s)",
        ENV_PROACTIVE_MODE,
        raw,
        ", ".join(m.value for m in PROACTIVE_MODES),
    )
    return ProactiveMode.OFF
