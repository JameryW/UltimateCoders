"""Stall signal surface + same-contract one-shot agent redispatch (P2 / D7).

Progress-stop / ``loop_detected`` is a first-class structured payload so
orchestrator / NATS consumers can react without scraping logs.

``UC_STALL_REDISPATCH_ENFORCE`` defaults **off**. When off, behaviour matches
today: stop without agent redispatch. When on, the orchestrator / dispatch
owner may redispatch the **same** contract digest once with an alternate
registered agent (ticket 14). A second stall (or no alternate) escalates to
non-retryable terminal. True replan (new decomposition) is deferred (T18).

The progress detector (``progress.py``) stays stop/retry evidence only — no
replan / agent-swap primitives live there.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from ultimate_coders.agent.progress import ProgressVerdict

STALL_REDISPATCH_ENFORCE_ENV = "UC_STALL_REDISPATCH_ENFORCE"

#: Reserved ``agent_config`` key holding :class:`StallEpisodeState` as a dict.
STALL_EPISODE_KEY = "_uc_stall_episode"

#: Observability stamp written beside the episode (never drives policy alone).
STALL_REDISPATCH_META_KEY = "_uc_stall_redispatch"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

_REDISPATCH_ACTIONS = frozenset({"noop", "swap_once", "escalate"})


@dataclass(frozen=True)
class StallSignal:
    """Structured progress-stop signal for events / failure_data payloads."""

    loop_detected: bool
    stalled_streak: int
    reason: str
    error_signature: str
    subtask_id: str | None = None
    contract_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for NATS / orchestrator event payloads.

        Omits optional identity fields when unset so consumers can key on
        presence without filtering nulls.
        """
        payload: dict[str, Any] = {
            "loop_detected": self.loop_detected,
            "stalled_streak": self.stalled_streak,
            "reason": self.reason,
            "error_signature": self.error_signature,
        }
        if self.subtask_id is not None:
            payload["subtask_id"] = self.subtask_id
        if self.contract_digest is not None:
            payload["contract_digest"] = self.contract_digest
        return payload


@dataclass(frozen=True)
class StallEpisodeState:
    """Per-subtask stall redispatch quota (exactly one swap per episode)."""

    swaps_used: int = 0
    last_agent: str | None = None
    contract_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"swaps_used": int(self.swaps_used)}
        if self.last_agent is not None:
            payload["last_agent"] = self.last_agent
        if self.contract_digest is not None:
            payload["contract_digest"] = self.contract_digest
        return payload


@dataclass(frozen=True)
class RedispatchDecision:
    """Outcome of :func:`decide_stall_redispatch`."""

    action: str  # "noop" | "swap_once" | "escalate"
    alternate_agent: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action not in _REDISPATCH_ACTIONS:
            raise ValueError(f"unknown redispatch action: {self.action!r}")


def stall_redispatch_enforce_enabled() -> bool:
    """Whether stall redispatch enforce is on; default off."""
    raw = os.environ.get(STALL_REDISPATCH_ENFORCE_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV


def build_stall_signal(
    verdict: ProgressVerdict,
    *,
    subtask_id: str | None = None,
    contract_digest: str | None = None,
) -> StallSignal | None:
    """Build a :class:`StallSignal` when ``verdict.action == "stop"``.

    Returns ``None`` for retry / non-stop verdicts so callers can attach the
    field only on terminal progress-stop without inventing a replan API.
    """
    if verdict.action != "stop":
        return None
    return StallSignal(
        loop_detected=True,
        stalled_streak=verdict.stalled_streak,
        reason=verdict.reason,
        error_signature=verdict.signature,
        subtask_id=subtask_id,
        contract_digest=contract_digest,
    )


def stall_signal_from_mapping(data: Mapping[str, Any] | None) -> StallSignal | None:
    """Parse a :class:`StallSignal` from failure_data / result payload."""
    if not isinstance(data, Mapping) or not data:
        return None
    reason = data.get("reason")
    error_signature = data.get("error_signature")
    if reason is None and error_signature is None:
        return None
    subtask_id = data.get("subtask_id")
    contract_digest = data.get("contract_digest")
    return StallSignal(
        loop_detected=bool(data.get("loop_detected", True)),
        stalled_streak=int(data.get("stalled_streak", 0) or 0),
        reason=str(reason or ""),
        error_signature=str(error_signature or ""),
        subtask_id=str(subtask_id) if subtask_id is not None else None,
        contract_digest=str(contract_digest) if contract_digest is not None else None,
    )


def load_stall_episode(agent_config: Mapping[str, Any] | None) -> StallEpisodeState:
    """Load episode state from ``agent_config`` (missing → fresh episode)."""
    raw = (agent_config or {}).get(STALL_EPISODE_KEY)
    if not isinstance(raw, Mapping):
        return StallEpisodeState()
    last_agent = raw.get("last_agent")
    contract_digest = raw.get("contract_digest")
    return StallEpisodeState(
        swaps_used=max(0, int(raw.get("swaps_used", 0) or 0)),
        last_agent=str(last_agent) if last_agent is not None else None,
        contract_digest=str(contract_digest) if contract_digest is not None else None,
    )


def current_agent_from_config(
    agent_config: Mapping[str, Any] | None,
    *,
    default: str | None = None,
) -> str:
    """Resolve the canonical coding-agent name from subtask config / env."""
    cfg = agent_config or {}
    for key in ("agent", "agent_name"):
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if isinstance(default, str) and default.strip():
        return default.strip()
    env = os.environ.get("UC_CODING_AGENT", "grok-build").strip()
    return env or "grok-build"


def select_alternate_agent(current: str, candidates: Sequence[str]) -> str | None:
    """First discoverable candidate ≠ ``current`` (stable list order)."""
    cur = (current or "").strip()
    for name in candidates:
        if not isinstance(name, str):
            continue
        n = name.strip()
        if n and n != cur:
            return n
    return None


def _digest_compatible(expected: str | None, signal_digest: str | None) -> bool:
    """Same-contract check: both set must match; either unset is permissive."""
    if not expected or not signal_digest:
        return True
    return expected == signal_digest


def decide_stall_redispatch(
    *,
    enforce: bool,
    stall: StallSignal | None,
    episode: StallEpisodeState,
    current_agent: str,
    available: Sequence[str],
    expected_digest: str | None = None,
) -> RedispatchDecision:
    """Pure policy: ``noop`` | ``swap_once`` | ``escalate``.

    Flag off / no stall → noop (today's terminal stop). Flag on + first stall
    + alternate + same digest → one redispatch. Second stall or no alternate
    → escalate. Does not mutate state or bump contract versions.
    """
    if not enforce or stall is None:
        return RedispatchDecision(
            action="noop",
            reason="stall redispatch enforce off or no stall signal",
        )

    if not _digest_compatible(expected_digest, stall.contract_digest):
        return RedispatchDecision(
            action="escalate",
            reason="contract digest mismatch; refuse same-contract redispatch",
        )

    if episode.swaps_used >= 1:
        return RedispatchDecision(
            action="escalate",
            reason="stall redispatch quota exhausted (one swap per episode)",
        )

    alternate = select_alternate_agent(current_agent, available)
    if alternate is None:
        return RedispatchDecision(
            action="escalate",
            reason="no alternate registered agent available",
        )

    return RedispatchDecision(
        action="swap_once",
        alternate_agent=alternate,
        reason="same-contract one-shot alternate agent redispatch",
    )


def apply_stall_redispatch_decision(
    subtask: Any,
    decision: RedispatchDecision,
    episode: StallEpisodeState,
) -> StallEpisodeState:
    """Apply a decision onto a subtask (orchestrator / dispatch thin wire).

    ``swap_once``: reopen to Pending, stamp ``agent_config["agent"]``, bump
    ``dispatch_retry_count``, persist episode with ``swaps_used=1``. Never
    rewrites ``user_request`` / goal text (digest identity preserved).

    ``escalate``: stamp observability meta only; caller marks terminal Failed.

    ``noop``: no mutation.
    """
    from ultimate_coders.agent.types import SubtaskStatus

    if decision.action == "noop":
        return episode

    cfg: MutableMapping[str, Any] = dict(getattr(subtask, "agent_config", None) or {})
    user_request = getattr(subtask, "user_request", "")

    if decision.action == "swap_once":
        if not decision.alternate_agent:
            raise ValueError("swap_once requires alternate_agent")
        new_episode = StallEpisodeState(
            swaps_used=max(1, int(episode.swaps_used) + 1),
            last_agent=decision.alternate_agent,
            contract_digest=episode.contract_digest,
        )
        cfg["agent"] = decision.alternate_agent
        cfg[STALL_EPISODE_KEY] = new_episode.to_dict()
        cfg[STALL_REDISPATCH_META_KEY] = {
            "action": "swap_once",
            "alternate_agent": decision.alternate_agent,
            "reason": decision.reason,
        }
        subtask.agent_config = dict(cfg)
        subtask.status = SubtaskStatus.PENDING
        subtask.assigned_worker = None
        subtask.result = None
        subtask.retry_count = 0
        subtask.dispatch_retry_count = int(getattr(subtask, "dispatch_retry_count", 0) or 0) + 1
        # Goal text / digest must stay identical (not contract_changed / T18).
        if getattr(subtask, "user_request", "") != user_request:
            subtask.user_request = user_request
        return new_episode

    # escalate — leave status to caller; record why we stopped auto-swaps.
    cfg[STALL_EPISODE_KEY] = episode.to_dict()
    cfg[STALL_REDISPATCH_META_KEY] = {
        "action": "escalate",
        "reason": decision.reason,
    }
    subtask.agent_config = dict(cfg)
    return episode


def maybe_apply_stall_redispatch(
    subtask: Any,
    *,
    stall: StallSignal | None,
    available: Sequence[str],
    enforce: bool | None = None,
    current_agent: str | None = None,
) -> RedispatchDecision:
    """Decide and apply ``swap_once`` / escalate meta on ``subtask``.

    On ``swap_once`` the subtask is already Pending with the alternate agent
    stamped — the caller must **not** overwrite status to Failed. On
    ``escalate`` / ``noop`` the caller marks Failed as usual.
    """
    if enforce is None:
        enforce = stall_redispatch_enforce_enabled()

    episode = load_stall_episode(getattr(subtask, "agent_config", None))
    expected: str | None = None
    user_request = getattr(subtask, "user_request", "") or ""
    if user_request:
        from ultimate_coders.agent.contract import contract_digest

        expected = contract_digest(user_request)
        if episode.contract_digest is None:
            episode = StallEpisodeState(
                swaps_used=episode.swaps_used,
                last_agent=episode.last_agent,
                contract_digest=expected,
            )

    agent = current_agent or current_agent_from_config(getattr(subtask, "agent_config", None))
    decision = decide_stall_redispatch(
        enforce=enforce,
        stall=stall,
        episode=episode,
        current_agent=agent,
        available=available,
        expected_digest=expected,
    )
    if decision.action in ("swap_once", "escalate"):
        apply_stall_redispatch_decision(subtask, decision, episode)
    return decision
