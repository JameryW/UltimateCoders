"""OverServe guard — polling amplification and completion evidence (v29 #137).

The v29 ledger found that long-horizon agents fail less from bad reasoning than
from *over-serving*: tool loops poll the same state key until the context fills,
and a completion claim is often written while the last observation still
contradicts it (72% of audited failures had counter-evidence and 88% of that
evidence was ignored).  This module is the Python counterpart of
``crates/uc-engine/src/scheduler/overserve_evidence_guard.rs`` and is the
production wiring point for the local harness tool loop.

Three deterministic rules, all bookkeeping (never executes a tool):

* **Poll throttle** — the first poll of a ``(tool, state_key)`` pair is free;
  repeats of the *same* key back off exponentially (``base * 2**n`` capped at
  ``max_delay_ms``), and once the state reported ``completed=True`` the key is
  broken: further polls are refused until :meth:`PollThrottle.reset`.
* **Counter-evidence** — a completion claim that arrives with contradicting
  observations is rolled back once, then acknowledged so a bounded loop cannot
  spin.  A claim with no counter-evidence passes immediately.
* **Provider crash fuse** — after a provider content-filter crash the framework
  must not synthesise a completion of its own; only a real model turn may.

Opt-in: :func:`overserve_guard_enabled` reads ``UC_OVERSERVE_GUARD`` and
defaults to off, so importing this module changes no harness behaviour.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_BASE_DELAY_MS",
    "DEFAULT_MAX_DELAY_MS",
    "DEFAULT_MAX_COMPLETION_BLOCKS",
    "OVERSERVE_GUARD_ENV",
    "CompletionSource",
    "CompletionVerdict",
    "CounterEvidenceGuard",
    "EvidenceVerdict",
    "OverServeGuard",
    "PollThrottle",
    "PollVerdict",
    "ProviderCrashFuse",
    "overserve_guard_enabled",
]

#: Environment flag that arms the guard in the local harness.
OVERSERVE_GUARD_ENV = "UC_OVERSERVE_GUARD"
#: First backoff step for a repeated poll of an unchanged state key.
DEFAULT_BASE_DELAY_MS = 500
#: Cap for the exponential poll backoff.
DEFAULT_MAX_DELAY_MS = 60_000
#: A completion claim may be rolled back at most this many times.
DEFAULT_MAX_COMPLETION_BLOCKS = 1
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def overserve_guard_enabled() -> bool:
    """Return whether the guard is armed via ``UC_OVERSERVE_GUARD``."""

    return os.environ.get(OVERSERVE_GUARD_ENV, "").strip().lower() in _TRUTHY


def state_key(tool: str, arguments: dict[str, object]) -> str:
    """Derive a stable key for the observed state of one tool call.

    Only the arguments matter -- the tool name is part of the key so two tools
    reading the same path do not share a backoff budget.  The digest keeps the
    key short and avoids leaking file contents into a prompt.
    """

    payload = json.dumps(arguments, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{tool}:{digest}"


@dataclass(frozen=True)
class PollVerdict:
    """Outcome of asking the throttle whether a poll may run now."""

    allow: bool
    delay_ms: int
    breaker_open: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "allow": self.allow,
            "delay_ms": self.delay_ms,
            "breaker_open": self.breaker_open,
            "reason": self.reason,
        }

    def to_tool_error(self) -> str:
        """Render the verdict as the JSON tool result the model will read."""

        if self.breaker_open:
            hint = (
                "This state already reported completion. Stop polling it and "
                "either verify a different state or finish the task."
            )
        else:
            hint = (
                f"Wait at least {self.delay_ms} ms before polling this exact "
                "state again, or poll a different state."
            )
        return json.dumps(
            {
                "error": f"poll throttled: {self.reason}",
                "delay_ms": self.delay_ms,
                "breaker_open": self.breaker_open,
                "hint": hint,
            }
        )


@dataclass
class PollThrottle:
    """Exponential backoff for repeated polls of an unchanged state key."""

    base_delay_ms: int = DEFAULT_BASE_DELAY_MS
    max_delay_ms: int = DEFAULT_MAX_DELAY_MS
    _counts: dict[str, int] = field(default_factory=dict, repr=False)
    _completed: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if self.base_delay_ms <= 0:
            raise ValueError("base_delay_ms must be positive")
        if self.max_delay_ms < self.base_delay_ms:
            raise ValueError("max_delay_ms must be >= base_delay_ms")

    def observe(self, key: str, *, completed: bool = False) -> PollVerdict:
        """Record one poll and decide whether it should run."""

        if not key:
            raise ValueError("state key must be non-empty")
        if key in self._completed:
            return PollVerdict(
                allow=False,
                delay_ms=0,
                breaker_open=True,
                reason="completed_state_breaker",
            )
        count = self._counts.get(key, 0)
        self._counts[key] = count + 1
        if completed:
            self._completed.add(key)
        if count == 0:
            return PollVerdict(
                allow=True,
                delay_ms=0,
                breaker_open=False,
                reason="first_poll",
            )
        delay = min(self.base_delay_ms * (2 ** (count - 1)), self.max_delay_ms)
        return PollVerdict(
            allow=False,
            delay_ms=delay,
            breaker_open=False,
            reason="repeated_unchanged_poll",
        )

    def count(self, key: str) -> int:
        return self._counts.get(key, 0)

    def is_completed(self, key: str) -> bool:
        return key in self._completed

    @property
    def completed_count(self) -> int:
        return len(self._completed)

    def reset(self) -> None:
        self._counts.clear()
        self._completed.clear()


@dataclass(frozen=True)
class EvidenceVerdict:
    """Outcome of a completion claim checked against counter-evidence."""

    claim_id: str
    accepted: bool
    rollback: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "accepted": self.accepted,
            "rollback": self.rollback,
            "reason": self.reason,
        }


@dataclass
class CounterEvidenceGuard:
    """Roll back a completion claim once per claim when evidence contradicts it.

    The guard never decides *whether* the contradicting observation is truly
    disqualifying -- it only refuses to let the claim pass silently.  The
    blocked attempt is surfaced to the model, which must then either fix the
    contradiction or restate the claim (the second attempt is acknowledged so
    the loop cannot spin forever).
    """

    max_blocks: int = DEFAULT_MAX_COMPLETION_BLOCKS
    _acknowledged: set[str] = field(default_factory=set, repr=False)
    _blocks: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.max_blocks < 0:
            raise ValueError("max_blocks must be >= 0")

    def evaluate(
        self,
        claim_id: str,
        *,
        supporting: tuple[str, ...] = (),
        contradicting: tuple[str, ...] = (),
    ) -> EvidenceVerdict:
        if not claim_id:
            raise ValueError("claim_id must be non-empty")
        if not contradicting:
            return EvidenceVerdict(
                claim_id=claim_id,
                accepted=True,
                rollback=False,
                reason="no_counter_evidence",
            )
        if claim_id in self._acknowledged or self._blocks >= self.max_blocks:
            return EvidenceVerdict(
                claim_id=claim_id,
                accepted=True,
                rollback=False,
                reason="contradiction_acknowledged",
            )
        self._acknowledged.add(claim_id)
        self._blocks += 1
        # ``supporting`` is recorded by the caller; this gate only needs what
        # contradicts the claim, so it is intentionally not stored.
        _ = supporting
        return EvidenceVerdict(
            claim_id=claim_id,
            accepted=False,
            rollback=True,
            reason="counter_evidence_present",
        )

    @property
    def blocks(self) -> int:
        return self._blocks

    def reset(self) -> None:
        self._acknowledged.clear()
        self._blocks = 0


@dataclass(frozen=True)
class CompletionVerdict:
    """Final answer for a ``done`` tool call."""

    allowed: bool
    rollback: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "rollback": self.rollback,
            "reason": self.reason,
        }

    def to_tool_error(self) -> str:
        return json.dumps(
            {
                "error": "completion claim contradicted by evidence",
                "reason": self.reason,
                "hint": (
                    "Counter-evidence is still present. Fix it, re-run the "
                    "failing check, then claim completion again."
                ),
            }
        )


class CompletionSource:
    """Who produced a completion: the real model or framework fallback."""

    MODEL = "model"
    FRAMEWORK_SYNTHESIS = "framework_synthesis"


@dataclass
class ProviderCrashFuse:
    """Refuse framework-synthesised completions after a provider crash."""

    _crashed: set[str] = field(default_factory=set, repr=False)

    def record_crash(self, provider: str) -> None:
        if not provider:
            raise ValueError("provider must be non-empty")
        self._crashed.add(provider)

    def crashed(self, provider: str) -> bool:
        return provider in self._crashed

    @property
    def crashed_count(self) -> int:
        return len(self._crashed)

    def completion_allowed(self, provider: str, source: str) -> bool:
        if source != CompletionSource.FRAMEWORK_SYNTHESIS:
            return True
        return not self.crashed(provider)

    def reset(self) -> None:
        self._crashed.clear()


@dataclass
class OverServeGuard:
    """Facade used by the local harness tool loop."""

    throttle: PollThrottle = field(default_factory=PollThrottle)
    evidence: CounterEvidenceGuard = field(default_factory=CounterEvidenceGuard)
    fuse: ProviderCrashFuse = field(default_factory=ProviderCrashFuse)
    polled_tools: frozenset[str] = frozenset({"list_dir", "read_file", "run_command"})
    #: True while the most recent command observation still failed.
    unresolved_failure: bool = False

    def gate_tool(
        self,
        tool: str,
        arguments: dict[str, object],
    ) -> str | None:
        """Return a rendered tool error when the poll must be refused."""

        if tool not in self.polled_tools:
            return None
        key = state_key(tool, arguments)
        verdict = self.throttle.observe(key)
        if verdict.allow:
            return None
        return verdict.to_tool_error()

    def notice_completed_state(
        self,
        tool: str,
        arguments: dict[str, object],
    ) -> None:
        """Mark a state key as completed so later polls break the loop."""

        if tool not in self.polled_tools:
            return
        key = state_key(tool, arguments)
        self.throttle.observe(key, completed=True)

    def observe_command_step(self, *, failed: bool) -> None:
        """Track whether the latest command observation still contradicts success.

        A later passing command clears the flag, so a fixed failure stops being
        counter-evidence instead of blocking every future completion claim.
        """

        self.unresolved_failure = bool(failed)

    def completion(
        self,
        claim_id: str,
        *,
        supporting: tuple[str, ...] = (),
        contradicting: tuple[str, ...] | None = None,
    ) -> CompletionVerdict:
        conflicts = (
            tuple(contradicting)
            if contradicting is not None
            else (
                ("unresolved_command_failure",) if self.unresolved_failure else ()
            )
        )
        if not conflicts and not self.throttle.completed_count:
            return CompletionVerdict(
                allowed=True,
                rollback=False,
                reason="clean",
            )
        verdict = self.evidence.evaluate(
            claim_id,
            supporting=supporting,
            contradicting=conflicts,
        )
        return CompletionVerdict(
            allowed=verdict.accepted,
            rollback=verdict.rollback,
            reason=verdict.reason,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "completed_states": self.throttle.completed_count,
            "completion_blocks": self.evidence.blocks,
            "crashed_providers": self.fuse.crashed_count,
            "unresolved_failure": self.unresolved_failure,
        }
