"""ECLoop evidence-conditioned execution gate (v10 #19).

The v10 architecture separates "the model is allowed to call the tool" from
"the model has earned the right to change this symbol".  Before an ``edit`` or
``submit`` action the worker must show that it inspected at least one 1-hop
relationship of the target -- a caller, an override, or a covering test.  The
gate is deliberately a pure in-process policy object: it never wraps or
intercepts the Rust tool executor and never inspects repository contents.  A
caller that wants enforcement calls :meth:`ECLoopGate.evaluate` before the
tool call and turns a denial into :meth:`ECLoopDecision.to_tool_error`.

Design constraints:

* Opt-in -- :func:`ecloop_gate_enabled` reads ``UC_ECLOOP_GATE`` and defaults
  to off, so importing this module changes nothing.
* Bounded -- a target may be blocked at most ``max_blocks`` times (3 by
  default).  After the budget is exhausted, ``strict=True`` keeps the gate
  closed because evidence is mandatory, while ``strict=False`` lets the action
  through with an explicit warning.
* Deterministic -- evidence matching is pure string containment, attempts are
  counted per ``file::symbol`` target, and the generated prompt is bounded.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_MAX_BLOCKS",
    "ECLOOP_GATE_ENV",
    "ECLoopDecision",
    "ECLoopGate",
    "EvidenceGap",
    "ecloop_gate_enabled",
]

#: Environment flag that turns the gate on in the worker.
ECLOOP_GATE_ENV = "UC_ECLOOP_GATE"
#: Default number of blocked attempts per target before the strict/lenient rule.
DEFAULT_MAX_BLOCKS = 3
#: Actions that require 1-hop evidence; everything else is read-only and free.
_GUARDED_ACTIONS = frozenset({"edit", "submit"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MAX_PROMPT_CHARS = 900


def ecloop_gate_enabled() -> bool:
    """Return whether the ECLoop gate is enabled via ``UC_ECLOOP_GATE``."""

    return os.environ.get(ECLOOP_GATE_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class EvidenceGap:
    """One missing-evidence explanation attached to a blocked decision.

    ``missing`` names the categories (with example candidates) that were
    probed but not found in the supplied evidence.  ``requested_action`` is
    the action that was blocked, so a caller can render the same gap for an
    ``edit`` or a ``submit`` without re-deriving it.
    """

    target: str
    missing: tuple[str, ...]
    urgency: str
    requested_action: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing", tuple(str(item) for item in self.missing))

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "missing": list(self.missing),
            "urgency": self.urgency,
            "requested_action": self.requested_action,
        }


@dataclass(frozen=True)
class ECLoopDecision:
    """Result of one gate evaluation.

    ``allowed`` is the only field a caller must branch on.  ``gap`` is set
    whenever evidence is missing (even when a lenient gate eventually allows
    the action), ``attempts`` is the number of blocks recorded for this
    target after this evaluation, and ``prompt`` is the bounded feedback text
    a worker can inject into the next model turn.
    """

    allowed: bool
    gap: EvidenceGap | None
    attempts: int
    prompt: str
    warning: str = ""

    def to_tool_error(self) -> str:
        """Render the denial as a tool-error string (empty when allowed).

        The Rust tool executor expects an error string; the worker can return
        this verbatim without exposing internal gate state.  Allowed decisions
        return ``""`` so callers can use truthiness.
        """

        if self.allowed:
            return ""
        if self.gap is None:  # defensive: a denial always carries a gap
            return "ECLoopGate blocked the action: missing 1-hop evidence."
        missing = ", ".join(self.gap.missing) or "1-hop evidence"
        message = (
            f"ECLoopGate blocked '{self.gap.requested_action}' on "
            f"{self.gap.target}: missing {missing}."
        )
        if self.prompt:
            message = f"{message} {self.prompt}"
        return message


class ECLoopGate:
    """In-process evidence gate for edit/submit actions.

    The gate is stateful only in one respect: it counts blocks per
    ``file::symbol`` target.  Pass one instance per worker attempt; sharing an
    instance across attempts would carry the block budget over.
    """

    def __init__(self, *, strict: bool = True, max_blocks: int = DEFAULT_MAX_BLOCKS) -> None:
        if int(max_blocks) < 1:
            raise ValueError("max_blocks must be >= 1")
        self.strict = bool(strict)
        self.max_blocks = int(max_blocks)
        self._blocks: dict[str, int] = {}

    @property
    def blocks(self) -> Mapping[str, int]:
        """Read-only snapshot of the per-target block counts."""

        return dict(self._blocks)

    def evaluate(
        self,
        target_file: str,
        ast_symbol: str,
        action: str,
        evidence: Sequence[str],
        *,
        callers: Iterable[str] = (),
        overrides: Iterable[str] = (),
        tests: Iterable[str] = (),
    ) -> ECLoopDecision:
        """Decide whether ``action`` on one symbol has enough 1-hop evidence.

        ``callers``, ``overrides`` and ``tests`` are the candidate 1-hop
        neighbours discovered by the code graph.  An evidence entry satisfies
        the gate when it contains (or is contained in) at least one candidate.
        When no candidate lists are supplied, any non-empty evidence counts:
        the caller declared the relationship itself.  Non-guarded actions are
        always allowed and do not consume the block budget.
        """

        target = _target_label(target_file, ast_symbol)
        normalised_action = str(action or "").strip().lower()
        attempts = self._blocks.get(target, 0)
        if normalised_action not in _GUARDED_ACTIONS:
            return ECLoopDecision(
                allowed=True,
                gap=None,
                attempts=attempts,
                prompt="",
            )

        missing = _missing_evidence(evidence, callers, overrides, tests)
        if not missing:
            return ECLoopDecision(
                allowed=True,
                gap=None,
                attempts=attempts,
                prompt="",
            )

        gap = EvidenceGap(
            target=target,
            missing=missing,
            urgency="high",
            requested_action=normalised_action,
        )
        if attempts >= self.max_blocks:
            if self.strict:
                return ECLoopDecision(
                    allowed=False,
                    gap=gap,
                    attempts=attempts,
                    prompt=_build_prompt(
                        gap,
                        strict=True,
                        attempts=attempts,
                        max_blocks=self.max_blocks,
                    ),
                    warning="strict mode: evidence remains mandatory after the block budget",
                )
            return ECLoopDecision(
                allowed=True,
                gap=gap,
                attempts=attempts,
                prompt=_build_prompt(
                    gap,
                    strict=False,
                    attempts=attempts,
                    max_blocks=self.max_blocks,
                ),
                warning=(
                    f"lenient mode: allowed after {attempts} blocked attempts "
                    "without evidence"
                ),
            )

        self._blocks[target] = attempts + 1
        return ECLoopDecision(
            allowed=False,
            gap=gap,
            attempts=attempts + 1,
            prompt=_build_prompt(
                gap,
                strict=self.strict,
                attempts=attempts + 1,
                max_blocks=self.max_blocks,
            ),
        )


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _target_label(target_file: str, ast_symbol: str) -> str:
    file_name = str(target_file or "").strip() or "<unknown>"
    symbol = str(ast_symbol or "").strip()
    return f"{file_name}::{symbol}" if symbol else file_name


def _missing_evidence(
    evidence: Sequence[str],
    callers: Iterable[str],
    overrides: Iterable[str],
    tests: Iterable[str],
) -> tuple[str, ...]:
    """Return the categories not represented by ``evidence`` (empty = allowed)."""

    entries = [str(item).strip().lower() for item in evidence if str(item).strip()]
    categories = (
        ("caller", tuple(str(value) for value in callers if str(value).strip())),
        ("override", tuple(str(value) for value in overrides if str(value).strip())),
        ("test", tuple(str(value) for value in tests if str(value).strip())),
    )
    available = [(name, values) for name, values in categories if values]
    if not entries:
        if not available:
            return ("1-hop caller/override/test evidence",)
        return tuple(_category_label(name, values) for name, values in available)
    if not available:
        # No candidate list was supplied: the caller's evidence stands on its own.
        return ()

    missing: list[str] = []
    matched = False
    for name, values in available:
        if any(_matches(entry, value) for entry in entries for value in values):
            matched = True
        else:
            missing.append(_category_label(name, values))
    return () if matched else tuple(missing)


def _category_label(name: str, values: tuple[str, ...]) -> str:
    preview = ", ".join(values[:3])
    if len(values) > 3:
        preview = f"{preview}, ..."
    return f"{name} ({preview})"


def _matches(entry: str, candidate: str) -> bool:
    haystack = candidate.strip().lower()
    if not haystack:
        return False
    return haystack in entry or entry in haystack


def _build_prompt(
    gap: EvidenceGap,
    *,
    strict: bool,
    attempts: int,
    max_blocks: int,
) -> str:
    """Bounded, actionable feedback for the next model turn."""

    missing = ", ".join(gap.missing) or "1-hop evidence"
    parts = [
        f"ECLoop evidence gate: '{gap.requested_action}' on {gap.target} "
        f"is blocked because no 1-hop evidence was supplied. Missing: {missing}.",
        "Inspect direct callers, method overrides, and covering tests; then retry "
        "with their names, paths, or tool-result ids in evidence.",
    ]
    if attempts >= max_blocks:
        if strict:
            parts.append(
                f"Block budget exhausted ({max_blocks}/{max_blocks}); strict mode keeps "
                "this action blocked until the missing evidence is provided."
            )
        else:
            parts.append(
                f"Block budget exhausted ({max_blocks}/{max_blocks}); lenient mode allows "
                "the action with a warning. Record the missing evidence in the attempt log."
            )
    else:
        parts.append(f"Blocked attempt {attempts} of {max_blocks} for this target.")
    prompt = " ".join(parts)
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = prompt[: _MAX_PROMPT_CHARS - 3].rstrip() + "..."
    return prompt
