"""ChainSWE chain isolation (#67).

The ledger's ChainSWE item observes that a multi-bug repair is a cumulative
chain: patch ``k`` runs on the repository state produced by patches ``1..k-1``.
An overshoot edit leaks unrelated changes into every later step; an undershoot
edit leaves a required symbol untouched. This module is the deterministic
bookkeeping half of the guard: it filters already-parsed edits against a bug's
minimum change boundary, reports overshoot/undershoot, estimates chain survival
and renders a compact change summary for the next bug.

Enable with ``UC_CHAIN_EDITOR=1``. No filesystem, git, model or network access.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

__all__ = [
    "BugScope",
    "ChainEditor",
    "ChainPatch",
    "ChangeSummary",
    "Edit",
    "SanitizedPatch",
    "chain_editor_enabled",
]

CHAIN_EDITOR_ENV = "UC_CHAIN_EDITOR"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def chain_editor_enabled() -> bool:
    """Return whether the opt-in chain editor is enabled."""

    return os.environ.get(CHAIN_EDITOR_ENV, "").strip().lower() in _TRUTHY


def _non_empty(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


@dataclass(frozen=True)
class Edit:
    """One AST-anchored edit."""

    path: str
    symbol: str
    added_lines: int
    removed_lines: int

    def __post_init__(self) -> None:
        _non_empty(self.path, "path")
        if self.added_lines < 0 or self.removed_lines < 0:
            raise ValueError("line counts must be non-negative")

    @property
    def changed_lines(self) -> int:
        return self.added_lines + self.removed_lines


@dataclass(frozen=True)
class BugScope:
    """Minimum change boundary for one bug."""

    bug_id: str
    paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    required_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.bug_id, "bug_id")
        if not self.paths and not self.symbols:
            raise ValueError("scope must name at least one path or symbol")

    def allows(self, edit: Edit) -> bool:
        return edit.path in self.paths or edit.symbol in self.symbols


@dataclass(frozen=True)
class ChainPatch:
    """The candidate patch for one bug."""

    bug_id: str
    edits: tuple[Edit, ...]

    def __post_init__(self) -> None:
        _non_empty(self.bug_id, "bug_id")


@dataclass(frozen=True)
class SanitizedPatch:
    """Scope-filtered patch."""

    bug_id: str
    kept: tuple[Edit, ...]
    stripped: tuple[Edit, ...]
    overshoot_lines: int
    overshoot_ratio: float
    required_missing: tuple[str, ...]
    survival: float

    @property
    def undershoot(self) -> bool:
        return bool(self.required_missing)

    @property
    def clean(self) -> bool:
        return not self.stripped and not self.undershoot

    @property
    def kept_lines(self) -> int:
        return sum(edit.changed_lines for edit in self.kept)


@dataclass(frozen=True)
class ChangeSummary:
    """Compact structured handoff to the next bug in the chain."""

    bug_id: str
    touched_symbols: tuple[str, ...]
    kept_lines: int
    stripped_lines: int
    text: str


@dataclass(frozen=True)
class ChainEditor:
    """Apply the minimum-change boundary to chained patches."""

    lambda_: float = 1.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.lambda_) or self.lambda_ < 0:
            raise ValueError("lambda must be finite and non-negative")

    def sanitize(self, patch: ChainPatch, scope: BugScope) -> SanitizedPatch:
        if patch.bug_id != scope.bug_id:
            raise ValueError(
                f"patch bug {patch.bug_id!r} does not match scope bug {scope.bug_id!r}"
            )
        if not patch.edits:
            raise ValueError("patch must contain at least one edit")

        kept: list[Edit] = []
        stripped: list[Edit] = []
        for edit in patch.edits:
            (kept if scope.allows(edit) else stripped).append(edit)

        total_lines = sum(edit.changed_lines for edit in patch.edits)
        overshoot_lines = sum(edit.changed_lines for edit in stripped)
        ratio = 0.0 if total_lines == 0 else overshoot_lines / total_lines
        kept_symbols = {edit.symbol for edit in kept if edit.symbol}
        missing = tuple(
            symbol for symbol in scope.required_symbols if symbol not in kept_symbols
        )
        survival = math.exp(-self.lambda_ * overshoot_lines)
        return SanitizedPatch(
            bug_id=patch.bug_id,
            kept=tuple(kept),
            stripped=tuple(stripped),
            overshoot_lines=overshoot_lines,
            overshoot_ratio=ratio,
            required_missing=missing,
            survival=survival,
        )

    def chain_survival(
        self, step_pass_rates: tuple[float, ...], overshoot_lines: int = 0
    ) -> float:
        if not step_pass_rates:
            raise ValueError("at least one step pass rate is required")
        product = 1.0
        for rate in step_pass_rates:
            if not math.isfinite(rate) or not 0.0 <= rate <= 1.0:
                raise ValueError("pass rates must be within [0, 1]")
            product *= rate
        return product * math.exp(-self.lambda_ * max(0, overshoot_lines))

    def summarize(self, sanitized: SanitizedPatch) -> ChangeSummary:
        symbols = tuple(
            sorted({edit.symbol for edit in sanitized.kept if edit.symbol})
        )
        kept = sanitized.kept_lines
        stripped = sanitized.overshoot_lines
        suffix = f"; stripped {stripped} overshoot lines" if stripped else ""
        if sanitized.required_missing:
            suffix += "; missing " + ", ".join(sanitized.required_missing)
        text = (
            f"Bug {sanitized.bug_id} kept {len(sanitized.kept)} edit(s) "
            f"({kept} lines) across {', '.join(symbols) or 'file scope'}{suffix}."
        )
        return ChangeSummary(
            bug_id=sanitized.bug_id,
            touched_symbols=symbols,
            kept_lines=kept,
            stripped_lines=stripped,
            text=text,
        )
