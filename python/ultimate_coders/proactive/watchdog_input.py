"""Watchdog input/output: deterministic stall evidence in, bounded action out.

The watchdog answers one question -- *what should happen to a task that is not
making progress?* -- and this module owns only the two ends of that conversation:

* **input**: :func:`build_stall_signal` turns the existing progress detector's
  verdict (``ultimate_coders.agent.progress``), the attempt/replan counters and
  the confirmed external effect ids (``ultimate_coders.effect_ledger``) into the
  exact ``watchdog.stall.report`` payload. Every number is copied, never
  invented: ``no_progress_count`` is the detector's stalled streak and
  ``evidence_signature`` is its normalized failure signature.
* **output**: :func:`apply_verdict` maps the gateway's verdict
  (``continue`` / ``retry_same_contract`` / ``replan`` / ``escalate_needs_human``
  / ``stop``) to one orchestrator action, and **refuses** a replan that does not
  carry every confirmed effect id. A replan that forgot a confirmed push or
  comment would repeat the side effect, so the recovery path is only taken when
  the evidence says it cannot.

Nothing here decides. The caps are read from ``UC_WATCHDOG_MAX_REPLANS``
(default 2), ``UC_WATCHDOG_MAX_ATTEMPTS`` (default 5) and
``UC_PROGRESS_STALL_LIMIT`` (default 2, the same semantics as the worker's
progress detector: unparseable -> default, values below 1 clamped). They mirror
the gateway's ``WatchdogConfig`` so both halves agree on what "bounded" means;
the gateway still owns the verdict, and this module never retries, sleeps or
replans by itself -- it returns the decision for the caller to act on.

Honest limitations:

* ``attempts_used`` / ``replans_used`` on the *request* are the caller's
  shadow counters. When ``UC_REPLAN_ACCOUNTING_ENFORCE`` is on and the stall
  carries a ``case_id``, the gateway ignores that shadow and returns
  authoritative ``replans_used`` / ``replans_remaining`` on the verdict —
  prefer those for any subsequent local bookkeeping.
* ``preserve_effects`` can only protect effects the caller *knows* are
  confirmed. An effect whose outcome is ``UNKNOWN`` in the ledger must be
  reconciled before a replan is attempted -- this module does not guess.
* No backoff, retry or scheduling lives here. ``delay_ms`` is forwarded to the
  caller, which is responsible for honouring it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ultimate_coders.agent.progress import ProgressVerdict

logger = logging.getLogger(__name__)

#: Maximum bounded replans per case (gateway ``WatchdogConfig.max_replans``).
ENV_WATCHDOG_MAX_REPLANS = "UC_WATCHDOG_MAX_REPLANS"
#: Maximum attempts per case before it is stopped outright.
ENV_WATCHDOG_MAX_ATTEMPTS = "UC_WATCHDOG_MAX_ATTEMPTS"
#: Consecutive no-evidence attempts before a stall is declared (shared with the
#: progress detector, which reads the same variable in ``agent/worker.py``).
ENV_PROGRESS_STALL_LIMIT = "UC_PROGRESS_STALL_LIMIT"
#: Gateway owns durable ``WorkCase.replans_used`` when truthy (T18c / D13).
#: Default off. Mirrored here for callers that prefer the returned counters.
REPLAN_ACCOUNTING_ENFORCE_ENV = "UC_REPLAN_ACCOUNTING_ENFORCE"

DEFAULT_STALL_LIMIT = 2
DEFAULT_MAX_REPLANS = 2
DEFAULT_MAX_ATTEMPTS = 5

#: Gateway verdict token -> orchestrator action. Unknown tokens are an error,
#: never a default: silently "continuing" on a verdict we cannot read would
#: turn a malformed answer into an unbounded loop.
VERDICT_ACTIONS: Mapping[str, str] = {
    "continue": "continue",
    "retry_same_contract": "retry_same_contract",
    "replan": "replan",
    "escalate_needs_human": "escalate",
    "stop": "stop",
}

#: Orchestrator actions :class:`WatchdogAction` may carry.
WATCHDOG_ACTIONS: tuple[str, ...] = (
    "continue",
    "retry_same_contract",
    "replan",
    "escalate",
    "stop",
)

#: Progress-detector actions :func:`build_stall_signal` understands.
_PROGRESS_ACTIONS = frozenset({"retry", "stop"})


class WatchdogContractError(ValueError):
    """The gateway's verdict contradicts the evidence the caller holds."""


def _non_negative_int(raw: Any, *, name: str, default: int) -> int:
    """Parse a counter. ``bool`` is rejected even though it is an ``int``."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}")
    if raw < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}")
    return raw


def _env_int(raw: str | None, *, name: str, default: int, minimum: int) -> int:
    """Read one integer env var, falling back loudly to the default."""
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("Watchdog env %s=%r is not a number; using %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning(
            "Watchdog env %s=%r is below the minimum %d; using %d", name, raw, minimum, minimum
        )
        return minimum
    return value


def progress_stall_limit(environ: Mapping[str, str] | None = None) -> int:
    """Consecutive no-evidence attempts before a stall is declared.

    Same semantics as the worker's ``_progress_stall_limit``: default 2, an
    unparseable value falls back to the default, and values below 1 are clamped
    (a limit of 0 would stop on the first repeat, including a repeat a single
    retry would have fixed).
    """
    source = os.environ if environ is None else environ
    return _env_int(
        source.get(ENV_PROGRESS_STALL_LIMIT),
        name=ENV_PROGRESS_STALL_LIMIT,
        default=DEFAULT_STALL_LIMIT,
        minimum=1,
    )


@dataclass(frozen=True)
class WatchdogLimits:
    """The Python mirror of the gateway's watchdog bounds.

    Used for the report threshold (below ``stall_limit`` there is no stall to
    report) and for display; the verdict itself is the gateway's.
    """

    stall_limit: int = DEFAULT_STALL_LIMIT
    max_replans: int = DEFAULT_MAX_REPLANS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WatchdogLimits:
        source = os.environ if environ is None else environ
        return cls(
            stall_limit=progress_stall_limit(source),
            max_replans=_env_int(
                source.get(ENV_WATCHDOG_MAX_REPLANS),
                name=ENV_WATCHDOG_MAX_REPLANS,
                default=DEFAULT_MAX_REPLANS,
                minimum=0,
            ),
            max_attempts=_env_int(
                source.get(ENV_WATCHDOG_MAX_ATTEMPTS),
                name=ENV_WATCHDOG_MAX_ATTEMPTS,
                default=DEFAULT_MAX_ATTEMPTS,
                minimum=0,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stall_limit": self.stall_limit,
            "max_replans": self.max_replans,
            "max_attempts": self.max_attempts,
        }


def _text(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _clean_effect_ids(raw: Any) -> tuple[str, ...]:
    """Normalize effect ids, preserving order and dropping duplicates.

    A non-string or blank id raises instead of being dropped: silently losing a
    confirmed effect id is how a recovery repeats a side effect.
    """
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Iterable):
        raise ValueError(f"effect ids must be a sequence of strings, got {raw!r}")
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"effect id must be a string, got {item!r}")
        value = item.strip()
        if not value:
            raise ValueError("effect ids must be non-empty strings")
        if value not in cleaned:
            cleaned.append(value)
    return tuple(cleaned)


def _build_payload(
    *,
    task_id: str,
    subtask_id: str | None,
    no_progress_count: int,
    evidence_signature: str | None,
    last_error: str | None,
    confirmed_effect_ids: Iterable[str],
    attempts_used: int,
    replans_used: int,
    already_retried: bool,
    case_id: str | None,
) -> dict[str, Any]:
    """Assemble the ``watchdog.stall.report`` payload.

    Optional identity fields are omitted when unset (the proto marks them
    ``optional``), so consumers can key on presence without filtering nulls.
    """
    task = _text(task_id)
    if not task:
        raise ValueError("task_id is required")
    payload: dict[str, Any] = {
        "task_id": task,
        "no_progress_count": _non_negative_int(
            no_progress_count, name="no_progress_count", default=0
        ),
        "confirmed_effect_ids": list(_clean_effect_ids(confirmed_effect_ids)),
        "attempts_used": _non_negative_int(attempts_used, name="attempts_used", default=0),
        "replans_used": _non_negative_int(replans_used, name="replans_used", default=0),
        "already_retried": bool(already_retried),
    }
    subtask = _text(subtask_id)
    if subtask:
        payload["subtask_id"] = subtask
    signature = _text(evidence_signature)
    if signature:
        payload["evidence_signature"] = signature
    error = _text(last_error)
    if error:
        payload["last_error"] = error
    case = _text(case_id)
    if case:
        payload["case_id"] = case
    return payload


def build_stall_signal(
    verdict: ProgressVerdict,
    *,
    task_id: str,
    subtask_id: str | None = None,
    last_error: str = "",
    confirmed_effect_ids: Iterable[str] = (),
    attempts_used: int = 0,
    replans_used: int = 0,
    already_retried: bool = False,
    case_id: str | None = None,
    limits: WatchdogLimits | None = None,
) -> dict[str, Any] | None:
    """Build the ``watchdog.stall.report`` payload for one progress verdict.

    Returns ``None`` when there is nothing to report: a ``retry`` verdict below
    the stall limit is normal progress, and sending it would only make the
    gateway answer ``continue``. A ``stop`` verdict is always reported -- the
    detector already declared the loop stalled.

    ``attempts_used`` / ``replans_used`` / ``already_retried`` are the caller's
    accounting of the case so far; they are forwarded unchanged so the gateway
    can decide against its own caps. ``confirmed_effect_ids`` must be the ids
    the effect ledger has *confirmed* -- the gateway echoes them into a replan's
    ``preserve_effects``.
    """
    action = _text(getattr(verdict, "action", ""))
    if action not in _PROGRESS_ACTIONS:
        raise ValueError(f"unknown progress verdict action: {action!r}")
    streak = _non_negative_int(
        getattr(verdict, "stalled_streak", 0), name="stalled_streak", default=0
    )
    active_limits = limits if limits is not None else WatchdogLimits.from_env()
    if action != "stop" and streak < active_limits.stall_limit:
        return None
    return _build_payload(
        task_id=task_id,
        subtask_id=subtask_id,
        no_progress_count=streak,
        evidence_signature=getattr(verdict, "signature", ""),
        last_error=last_error,
        confirmed_effect_ids=confirmed_effect_ids,
        attempts_used=attempts_used,
        replans_used=replans_used,
        already_retried=already_retried,
        case_id=case_id,
    )


def normalize_stall_report(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an externally supplied stall report.

    The dashboard endpoint receives a report from an orchestrator or an operator;
    this turns it into the same canonical payload :func:`build_stall_signal`
    produces, raising :class:`ValueError` on a malformed field. It is shape
    validation only -- no cap is applied and no verdict is predicted here.
    """
    if not isinstance(body, Mapping):
        raise ValueError("stall report must be an object")
    already_retried = body.get("already_retried", False)
    if not isinstance(already_retried, bool):
        raise ValueError(f"already_retried must be a boolean, got {already_retried!r}")
    return _build_payload(
        task_id=body.get("task_id", ""),
        subtask_id=body.get("subtask_id"),
        no_progress_count=body.get("no_progress_count", 0),
        evidence_signature=body.get("evidence_signature"),
        last_error=body.get("last_error"),
        confirmed_effect_ids=body.get("confirmed_effect_ids", ()),
        attempts_used=body.get("attempts_used", 0),
        replans_used=body.get("replans_used", 0),
        already_retried=already_retried,
        case_id=body.get("case_id"),
    )


@dataclass(frozen=True)
class WatchdogAction:
    """One orchestrator action derived from a gateway verdict."""

    action: str
    delay_ms: int = 0
    preserve_effects: tuple[str, ...] = ()
    attempt: int | None = None
    reason: str | None = None
    replans_remaining: int = 0
    attempts_remaining: int = 0
    #: Authoritative gateway counter when present on the verdict (T18c).
    replans_used: int | None = None

    def __post_init__(self) -> None:
        if self.action not in WATCHDOG_ACTIONS:
            raise ValueError(f"unknown watchdog action: {self.action!r}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "delay_ms": self.delay_ms,
            "preserve_effects": list(self.preserve_effects),
            "replans_remaining": self.replans_remaining,
            "attempts_remaining": self.attempts_remaining,
        }
        if self.attempt is not None:
            payload["attempt"] = self.attempt
        if self.reason:
            payload["reason"] = self.reason
        if self.replans_used is not None:
            payload["replans_used"] = self.replans_used
        return payload


def _contract_int(report: Mapping[str, Any], name: str, default: int) -> int:
    raw = report.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise WatchdogContractError(f"{name} must be a non-negative integer, got {raw!r}")
    return raw


def _contract_effects(raw: Any, *, name: str) -> tuple[str, ...]:
    try:
        return _clean_effect_ids(raw)
    except ValueError as exc:
        raise WatchdogContractError(f"{name}: {exc}") from exc


def apply_verdict(
    report: Mapping[str, Any],
    *,
    confirmed_effect_ids: Iterable[str] = (),
) -> WatchdogAction:
    """Map one ``watchdog.stall.report`` answer to an orchestrator action.

    The gateway owns the verdict; this function only translates it and enforces
    the invariants a recovery path must not lose:

    * a ``replan`` must carry ``preserve_effects`` covering every confirmed
      effect id the caller holds -- otherwise it raises
      :class:`WatchdogContractError` instead of handing back a replan that could
      repeat a push or a comment;
    * a ``replan`` must carry an ``attempt`` number. With durable accounting
      (T18c) the last allowed replan may return ``replans_remaining=0`` after
      the gateway records the increment — trust the gateway verdict rather than
      inventing a local escalate.

    When the reply includes ``replans_used``, prefer that counter over any
    local shadow (especially when ``UC_REPLAN_ACCOUNTING_ENFORCE`` is on).

    An unreadable verdict token raises as well: treating it as ``continue``
    would let a malformed response run forever.
    """
    if not isinstance(report, Mapping):
        raise WatchdogContractError(f"watchdog verdict must be an object, got {report!r}")
    raw_verdict = report.get("verdict")
    action = VERDICT_ACTIONS.get(raw_verdict) if isinstance(raw_verdict, str) else None
    if action is None:
        raise WatchdogContractError(
            f"unknown watchdog verdict {raw_verdict!r}; expected one of "
            + ", ".join(sorted(VERDICT_ACTIONS))
        )
    replans_remaining = _contract_int(report, "replans_remaining", 0)
    attempts_remaining = _contract_int(report, "attempts_remaining", 0)
    replans_used = None
    if "replans_used" in report and report.get("replans_used") is not None:
        replans_used = _contract_int(report, "replans_used", 0)

    if action == "continue":
        return WatchdogAction(
            action="continue",
            replans_remaining=replans_remaining,
            attempts_remaining=attempts_remaining,
            replans_used=replans_used,
        )

    if action == "retry_same_contract":
        return WatchdogAction(
            action="retry_same_contract",
            delay_ms=_contract_int(report, "delay_ms", 0),
            replans_remaining=replans_remaining,
            attempts_remaining=attempts_remaining,
            replans_used=replans_used,
        )

    if action == "replan":
        attempt = report.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise WatchdogContractError(f"replan verdict without a valid attempt: {attempt!r}")
        preserve = _contract_effects(report.get("preserve_effects"), name="preserve_effects")
        confirmed = _contract_effects(confirmed_effect_ids, name="confirmed_effect_ids")
        missing = [effect for effect in confirmed if effect not in preserve]
        if missing:
            raise WatchdogContractError(
                "replan does not preserve confirmed external effect(s): " + ", ".join(missing)
            )
        return WatchdogAction(
            action="replan",
            delay_ms=_contract_int(report, "delay_ms", 0),
            preserve_effects=preserve,
            attempt=attempt,
            reason=_text(report.get("reason")) or None,
            replans_remaining=replans_remaining,
            attempts_remaining=attempts_remaining,
            replans_used=replans_used,
        )

    reason = _text(report.get("reason"))
    return WatchdogAction(
        action=action,
        reason=reason or None,
        replans_remaining=replans_remaining,
        attempts_remaining=attempts_remaining,
        replans_used=replans_used,
    )
