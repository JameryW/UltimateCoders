"""Bounded Recovery View for a retry attempt.

The old retry loop re-used the *same* context block and handed the next
attempt only a free-text ``last_failure`` string. A recovery view instead
renders the committed facts of the prior attempt(s) — what was changed, how
the attempt ended, what the verification said, and which root-cause guesses
are still unverified — inside a token budget small enough to ride along on
every retry (plan: 300-600 tokens, ``UC_RECOVERY_BUDGET_TOKENS`` default 600).

Two rules shape the output (plan principles A and C):

* **Facts vs interpretation.** Patch/outcome/verdict facts come from the
  attempt ledger and render as verified items. A root-cause hypothesis is an
  interpretation: it renders as an *unverified* item so the next attempt can
  test it, not inherit it as truth.
* **Attempt-local by default.** The view describes only this subtask's retry
  chain. Cross-graph/page knowledge is a later stage (``rollout`` GRAPH /
  CANONICAL) and never arrives through this function.

The builder is pure: callers pass the rounds/evidence/hypotheses they already
read through the ledgers, so this module needs no database and is trivially
testable. ``build_recovery_view`` never raises on malformed records; it keeps
what it can parse, and reports truncation in the summary.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ultimate_coders.agent.context_compiler import ContextItem, Tier, estimate_tokens
from ultimate_coders.knowledge import rollout
from ultimate_coders.knowledge.model import AttemptRef, safe_text

#: Source tag recorded on the produced context items (provenance in the
#: manifest renderer and the audit trail).
RECOVERY_SOURCE = "recovery-ledger"

#: Section header the retry prompt sees.
RECOVERY_TITLE = "## Prior attempt evidence"

#: Changed paths shown per round before collapsing to "... and N more".
MAX_PATHS_PER_ROUND = 6

#: Failure/verdict text kept per round (chars, ``safe_text`` bound).
MAX_ROUND_DETAIL = 240

#: Rounds considered when the caller does not pass ``max_rounds``.
DEFAULT_MAX_ROUNDS = 5

#: Hypotheses rendered at most.
MAX_HYPOTHESES = 3

_FACT_TRUST = "attempt_ledger"
_HYPOTHESIS_TRUST = "unverified_hypothesis"


@dataclass(frozen=True)
class RecoveryView:
    """Rendered, budget-bounded recovery facts for one retry attempt."""

    attempt: AttemptRef = field(default_factory=AttemptRef)
    rounds: tuple[dict[str, Any], ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    hypotheses: tuple[dict[str, Any], ...] = ()
    facts_text: str = ""
    hypotheses_text: str = ""
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Full prompt text (facts section then unverified hypotheses)."""
        return "\n\n".join(part for part in (self.facts_text, self.hypotheses_text) if part)

    def is_empty(self) -> bool:
        return not self.text

    def tokens(self) -> int:
        return estimate_tokens(self.text) if self.text else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt.to_dict(),
            "rounds": list(self.rounds),
            "evidence": list(self.evidence),
            "hypotheses": list(self.hypotheses),
            "facts_text": self.facts_text,
            "hypotheses_text": self.hypotheses_text,
            "summary": dict(self.summary),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecoveryView:
        data = data or {}
        return cls(
            attempt=AttemptRef.from_dict(data.get("attempt") or {}),
            rounds=tuple(data.get("rounds") or ()),
            evidence=tuple(data.get("evidence") or ()),
            hypotheses=tuple(data.get("hypotheses") or ()),
            facts_text=str(data.get("facts_text") or ""),
            hypotheses_text=str(data.get("hypotheses_text") or ""),
            summary=dict(data.get("summary") or {}),
        )


def build_recovery_view(
    *,
    attempt: AttemptRef,
    rounds: Iterable[dict[str, Any]] | None = None,
    evidence: Iterable[dict[str, Any]] | None = None,
    hypotheses: Iterable[dict[str, Any]] | None = None,
    budget_tokens: int | None = None,
    max_rounds: int | None = None,
) -> RecoveryView:
    """Render prior-attempt facts into a bounded, log-safe recovery view.

    ``rounds`` are ``AttemptRound.to_dict()`` payloads (or equivalent dicts)
    already ordered oldest-first; the most recent ``max_rounds`` are shown and
    the oldest are dropped first when the budget is tight. Unknown fields are
    ignored rather than fatal — a partially migrated ledger still helps the
    next attempt.
    """
    budget = max(
        128,
        int(
            budget_tokens
            if budget_tokens is not None
            else rollout.recovery_budget_tokens()
        ),
    )
    char_budget = budget * 4  # heuristic token ≈ 4 chars; see estimate_tokens
    limit = max(1, int(max_rounds if max_rounds is not None else DEFAULT_MAX_ROUNDS))

    round_list = [r for r in (rounds or ()) if isinstance(r, dict)]
    evidence_list = [e for e in (evidence or ()) if isinstance(e, dict)]
    hypothesis_list = [h for h in (hypotheses or ()) if isinstance(h, dict)]
    selected_rounds = round_list[-limit:]
    truncated = len(round_list) > len(selected_rounds)

    # Hypotheses first: they are short, and if the budget cannot fit them the
    # facts still win. Facts are rendered in the remaining budget as worst case
    # comes last? No -- newest round matters most, so render newest-first into a
    # temporary list, then restore chronological order for the prompt.
    hypothesis_lines: list[str] = []
    for item in hypothesis_list[:MAX_HYPOTHESES]:
        statement = safe_text(
            item.get("statement") or item.get("summary") or "",
            limit=MAX_ROUND_DETAIL,
        )
        if not statement:
            continue
        hypothesis_lines.append(f"- {statement}")
    hypotheses_text = ""
    if hypothesis_lines:
        hypotheses_text = (
            "Unverified hypotheses from prior attempts (test, do not assume):\n"
            + "\n".join(hypothesis_lines)
        )
    hypothesis_room = len(hypotheses_text) + 20  # section separator + margin

    fact_blocks: list[str] = []
    used = len(RECOVERY_TITLE) + 1
    for offset, record in enumerate(reversed(selected_rounds)):
        block = _render_round(record)
        if not block:
            continue
        if used + len(block) + hypothesis_room > char_budget:
            truncated = True
            break
        fact_blocks.append(block)
        used += len(block) + 2
    facts_text = ""
    if fact_blocks:
        facts_text = RECOVERY_TITLE + "\n" + "\n\n".join(reversed(fact_blocks))
    if hypotheses_text and used + len(hypotheses_text) > char_budget:
        # Hypotheses lost the budget race: facts are the load-bearing part.
        hypotheses_text = ""
        truncated = True

    combined_text = facts_text + hypotheses_text
    summary = {
        "attempt": attempt.key(),
        "rounds_shown": len(fact_blocks),
        "rounds_available": len(round_list),
        "evidence_records": len(evidence_list),
        "hypotheses_shown": hypotheses_text.count("\n- ") if hypotheses_text else 0,
        "tokens": estimate_tokens(combined_text) if combined_text else 0,
        "budget_tokens": budget,
        "truncated": truncated,
    }
    return RecoveryView(
        attempt=attempt,
        rounds=tuple(selected_rounds if fact_blocks else ()),
        evidence=tuple(evidence_list),
        hypotheses=tuple(hypothesis_list[:MAX_HYPOTHESES] if hypotheses_text else ()),
        facts_text=facts_text,
        hypotheses_text=hypotheses_text,
        summary=summary,
    )


def recovery_context_items(
    view: RecoveryView, *, source: str = RECOVERY_SOURCE
) -> list[ContextItem]:
    """Turn a recovery view into compiler items (facts verified, guesses not).

    Two items, not one: the compiler's ``verified`` flag is per item, and a
    hypothesis must never inherit the verified status of the patch facts it
    sits next to. Both are P2 evidence so a high-risk attempt's reservation
    protects them; the facts item carries relevance 1.0 (directly actionable),
    the hypothesis item 0.6.
    """
    items: list[ContextItem] = []
    if view.facts_text:
        items.append(
            ContextItem(
                item_id=f"recovery-facts:{view.attempt.key()}",
                tier=Tier.EVIDENCE,
                content=view.facts_text,
                source=source,
                source_version="failed",
                trust_class=_FACT_TRUST,
                verified=True,
                relevance=1.0,
            )
        )
    if view.hypotheses_text:
        items.append(
            ContextItem(
                item_id=f"recovery-hypotheses:{view.attempt.key()}",
                tier=Tier.EVIDENCE,
                content=view.hypotheses_text,
                source=source,
                source_version="failed",
                trust_class=_HYPOTHESIS_TRUST,
                verified=False,
                relevance=0.6,
            )
        )
    return items


def _render_round(record: dict[str, Any]) -> str:
    """Render one attempt round as a compact section ("" when unusable)."""
    attempt = record.get("attempt") or {}
    if isinstance(attempt, dict):
        attempt_label = _attempt_label(attempt)
    else:
        attempt_label = safe_text(str(attempt), limit=80) or "attempt"
    outcome = str(record.get("outcome") or "unknown")
    lines = [f"### {attempt_label} — {outcome}"]
    paths = _changed_paths(record)
    if paths:
        shown = ", ".join(paths[:MAX_PATHS_PER_ROUND])
        if len(paths) > MAX_PATHS_PER_ROUND:
            shown += f", ... and {len(paths) - MAX_PATHS_PER_ROUND} more"
        lines.append(f"Changed: {shown}")
    base = str(record.get("base_revision") or "").strip()
    head = str(record.get("head_revision") or "").strip()
    if base and head:
        lines.append(f"Revision: {base[:12]} -> {head[:12]}")
    failure = safe_text(record.get("failure") or "", limit=MAX_ROUND_DETAIL)
    if failure:
        lines.append(f"Failure: {failure}")
    verdict = safe_text(record.get("test_verdict") or "", limit=MAX_ROUND_DETAIL)
    if verdict:
        lines.append(f"Verification: {verdict}")
    summary = safe_text(record.get("summary") or "", limit=MAX_ROUND_DETAIL)
    if summary:
        lines.append(f"Summary: {summary}")
    return "\n".join(lines)


def _attempt_label(attempt: dict[str, Any]) -> str:
    subtask = str(attempt.get("subtask_id") or "?")
    dispatch = attempt.get("dispatch_attempt", 0)
    retry = attempt.get("attempt", 0)
    return f"Attempt {retry} (dispatch {dispatch}) of subtask {subtask[:8]}"


def _changed_paths(record: dict[str, Any]) -> list[str]:
    """Extract changed paths from a round's serialized patch list."""
    paths: list[str] = []
    patches: Sequence[Any] = record.get("patches") or ()
    for patch in patches:
        if isinstance(patch, dict):
            path = patch.get("path")
        else:
            path = getattr(patch, "path", None)
        text = str(path or "").strip()
        if text:
            paths.append(text)
    changed = record.get("changed_paths")
    if isinstance(changed, (list, tuple)):
        for path in changed:
            text = str(path or "").strip()
            if text and text not in paths:
                paths.append(text)
    return paths
