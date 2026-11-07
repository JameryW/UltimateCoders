"""Same-contract one-shot coding Champion/Challenger (V8 P2 / D8).

After a coding subtask **succeeds**, optionally reopen once with an
alternate registered agent stamped ``eval_role=report`` for compare /
observe. ``UC_CODING_CHALLENGER_ENFORCE`` defaults **off**.

MVP is report-role only — no auto-promote of challenger over champion.
Stall-path agent swap remains ``UC_STALL_REDISPATCH_ENFORCE`` (T14); this
module does not replace it and does not run on failure/stall.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from ultimate_coders.agent.stall_redispatch import (
    current_agent_from_config,
    select_alternate_agent,
)

CODING_CHALLENGER_ENFORCE_ENV = "UC_CODING_CHALLENGER_ENFORCE"
CODING_CHALLENGER_AGENT_ENV = "UC_CODING_CHALLENGER_AGENT"

#: Reserved ``agent_config`` key holding :class:`ChallengerEpisodeState` as a dict.
CHALLENGER_EPISODE_KEY = "_uc_challenger_episode"

#: Observability stamp written beside the episode (never drives policy alone).
CHALLENGER_META_KEY = "_uc_challenger"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

_CHALLENGER_ACTIONS = frozenset({"noop", "challenge_once"})


@dataclass(frozen=True)
class ChallengerEpisodeState:
    """Per-subtask challenger quota (exactly one challenge per episode)."""

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
class ChallengerDecision:
    """Outcome of :func:`decide_coding_challenger`."""

    action: str  # "noop" | "challenge_once"
    challenger_agent: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action not in _CHALLENGER_ACTIONS:
            raise ValueError(f"unknown challenger action: {self.action!r}")


def coding_challenger_enforce_enabled() -> bool:
    """Whether coding challenger enforce is on; default off."""
    raw = os.environ.get(CODING_CHALLENGER_ENFORCE_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV


def preferred_challenger_agent() -> str | None:
    """Optional preferred challenger from ``UC_CODING_CHALLENGER_AGENT``."""
    raw = os.environ.get(CODING_CHALLENGER_AGENT_ENV, "").strip()
    return raw or None


def load_challenger_episode(agent_config: Mapping[str, Any] | None) -> ChallengerEpisodeState:
    """Load episode state from ``agent_config`` (missing → fresh episode)."""
    raw = (agent_config or {}).get(CHALLENGER_EPISODE_KEY)
    if not isinstance(raw, Mapping):
        return ChallengerEpisodeState()
    last_agent = raw.get("last_agent")
    contract_digest = raw.get("contract_digest")
    return ChallengerEpisodeState(
        swaps_used=max(0, int(raw.get("swaps_used", 0) or 0)),
        last_agent=str(last_agent) if last_agent is not None else None,
        contract_digest=str(contract_digest) if contract_digest is not None else None,
    )


def _digest_compatible(expected: str | None, episode_digest: str | None) -> bool:
    """Same-contract check: both set must match; either unset is permissive."""
    if not expected or not episode_digest:
        return True
    return expected == episode_digest


def _pick_challenger(
    current: str,
    available: Sequence[str],
    preferred: str | None,
) -> str | None:
    """Prefer ``preferred`` when valid; else first registry alternate ≠ current."""
    cur = (current or "").strip()
    names = [n.strip() for n in available if isinstance(n, str) and n.strip()]
    if preferred is not None:
        p = preferred.strip()
        if p and p != cur and p in names:
            return p
    return select_alternate_agent(cur, names)


def decide_coding_challenger(
    *,
    enforce: bool,
    success: bool,
    episode: ChallengerEpisodeState,
    current_agent: str,
    available: Sequence[str],
    preferred: str | None = None,
    expected_digest: str | None = None,
) -> ChallengerDecision:
    """Pure policy: ``noop`` | ``challenge_once``.

    Flag off / not success → noop (today's Completed path). Flag on + first
    success + alternate + same digest → one report-role challenger. Quota
    exhausted or no alternate → noop (keep champion Completed). Does not
    mutate state or rewrite goal text.
    """
    if not enforce or not success:
        return ChallengerDecision(
            action="noop",
            reason="coding challenger enforce off or not a success",
        )

    if not _digest_compatible(expected_digest, episode.contract_digest):
        return ChallengerDecision(
            action="noop",
            reason="contract digest mismatch; refuse same-contract challenger",
        )

    if episode.swaps_used >= 1:
        return ChallengerDecision(
            action="noop",
            reason="coding challenger quota exhausted (one challenge per episode)",
        )

    challenger = _pick_challenger(current_agent, available, preferred)
    if challenger is None:
        return ChallengerDecision(
            action="noop",
            reason="no alternate registered agent available",
        )

    return ChallengerDecision(
        action="challenge_once",
        challenger_agent=challenger,
        reason="same-contract one-shot report-role challenger",
    )


def apply_coding_challenger_decision(
    subtask: Any,
    decision: ChallengerDecision,
    episode: ChallengerEpisodeState,
    *,
    champion_summary: str | None = None,
    champion_agent: str | None = None,
) -> ChallengerEpisodeState:
    """Apply a decision onto a subtask (orchestrator / dispatch thin wire).

    ``challenge_once``: reopen to Pending, stamp ``agent_config["agent"]`` and
    ``eval_role=report``, stash champion compare fields in meta, bump
    ``dispatch_retry_count``, persist episode with ``swaps_used=1``. Never
    rewrites ``user_request`` / goal text (digest identity preserved).

    ``noop``: no mutation.
    """
    from ultimate_coders.agent.types import SubtaskStatus

    if decision.action == "noop":
        return episode

    if decision.action != "challenge_once":
        return episode

    if not decision.challenger_agent:
        raise ValueError("challenge_once requires challenger_agent")

    cfg: MutableMapping[str, Any] = dict(getattr(subtask, "agent_config", None) or {})
    user_request = getattr(subtask, "user_request", "")
    champ = champion_agent or current_agent_from_config(cfg)

    new_episode = ChallengerEpisodeState(
        swaps_used=max(1, int(episode.swaps_used) + 1),
        last_agent=decision.challenger_agent,
        contract_digest=episode.contract_digest,
    )
    cfg["agent"] = decision.challenger_agent
    cfg["eval_role"] = "report"
    cfg[CHALLENGER_EPISODE_KEY] = new_episode.to_dict()
    meta: dict[str, Any] = {
        "action": "challenge_once",
        "challenger_agent": decision.challenger_agent,
        "champion_agent": champ,
        "reason": decision.reason,
    }
    if champion_summary is not None:
        meta["champion_summary"] = champion_summary
    cfg[CHALLENGER_META_KEY] = meta
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


def maybe_apply_coding_challenger(
    subtask: Any,
    *,
    success: bool,
    available: Sequence[str],
    enforce: bool | None = None,
    preferred: str | None = None,
    current_agent: str | None = None,
    champion_summary: str | None = None,
) -> ChallengerDecision:
    """Decide and apply ``challenge_once`` on ``subtask`` after a success.

    On ``challenge_once`` the subtask is already Pending with the challenger
    agent and ``eval_role=report`` stamped — the caller must **not** overwrite
    status to Completed. On ``noop`` the caller marks Completed as usual.
    """
    if enforce is None:
        enforce = coding_challenger_enforce_enabled()
    if preferred is None:
        preferred = preferred_challenger_agent()

    episode = load_challenger_episode(getattr(subtask, "agent_config", None))
    expected: str | None = None
    user_request = getattr(subtask, "user_request", "") or ""
    if user_request:
        from ultimate_coders.agent.contract import contract_digest

        expected = contract_digest(user_request)
        if episode.contract_digest is None:
            episode = ChallengerEpisodeState(
                swaps_used=episode.swaps_used,
                last_agent=episode.last_agent,
                contract_digest=expected,
            )

    agent = current_agent or current_agent_from_config(getattr(subtask, "agent_config", None))
    decision = decide_coding_challenger(
        enforce=enforce,
        success=success,
        episode=episode,
        current_agent=agent,
        available=available,
        preferred=preferred,
        expected_digest=expected,
    )
    if decision.action == "challenge_once":
        apply_coding_challenger_decision(
            subtask,
            decision,
            episode,
            champion_summary=champion_summary,
            champion_agent=agent,
        )
    return decision


def is_report_challenger_episode(agent_config: Mapping[str, Any] | None) -> bool:
    """True when this subtask is finishing a stamped report-role challenger run.

    Quota already used + ``eval_role=report`` means the in-flight attempt is the
    observe-only challenger (ticket 08 / D8), not a fresh champion success.
    """
    cfg = agent_config or {}
    if cfg.get("eval_role") != "report":
        return False
    return load_challenger_episode(cfg).swaps_used >= 1


def finalize_coding_challenger_observation(subtask: Any, result: Any) -> Any:
    """Record challenger outcome; keep champion authoritative on report failure.

    Observe-only MVP (D8): a failed report-role challenger must **not** demote
    the subtask to Failed / trigger stall redispatch. Meta keeps both sides for
    compare; the attached result stays ``success=True`` with the champion
    summary when the challenger failed. Challenger success keeps its own result
    (still no auto-promote of artifacts — consumers compare via meta).
    """
    cfg: MutableMapping[str, Any] = dict(getattr(subtask, "agent_config", None) or {})
    meta: dict[str, Any] = dict(cfg.get(CHALLENGER_META_KEY) or {})
    challenger_ok = bool(getattr(result, "success", False))
    meta["challenger_success"] = challenger_ok
    summary = getattr(result, "summary", None)
    if summary is not None:
        meta["challenger_summary"] = summary
    err = getattr(result, "error", None) or ""
    if err:
        meta["challenger_error"] = str(err)
    cfg[CHALLENGER_META_KEY] = meta
    subtask.agent_config = dict(cfg)

    if challenger_ok:
        return result

    from ultimate_coders.agent.types import SubtaskResult

    champion_summary = meta.get("champion_summary") or (
        "champion succeeded; challenger report failed"
    )
    return SubtaskResult(
        subtask_id=getattr(result, "subtask_id", "") or getattr(subtask, "id", ""),
        worker_id=getattr(result, "worker_id", "") or "",
        success=True,
        summary=str(champion_summary),
        retryable=False,
        error="",
    )
