"""Budgeted graph-knowledge read face (P5).

Claims promoted to GRAPH or REPOSITORY visibility may inform other attempts in
the same task graph, but only through the Epistemic Gate's read rules and a
bounded token budget. This module is the production consumer of
``EvidenceLedger.visible()``: it returns permission-filtered claims and compiles
them into ``ContextItem`` rows the unified context compiler can place in the
next prompt.

Two honest constraints stay visible:

* disabled/earlier stages return nothing (stage ``graph`` is the first stage
  that lets graph-shared knowledge into a prompt);
* the summary counts claims and tokens only - never claim statements - so the
  read face is log-safe like every other knowledge-layer hook.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ultimate_coders.agent.context_compiler import ContextItem, Tier, estimate_tokens
from ultimate_coders.knowledge import rollout
from ultimate_coders.knowledge.evidence import ClaimRecord, EvidenceLedger
from ultimate_coders.knowledge.model import (
    ClaimStatus,
    KnowledgeScope,
    Visibility,
    safe_text,
    visibility_rank,
)

RETRIEVAL_SOURCE = "knowledge-claims"
MAX_CLAIMS = 20
MAX_STATEMENT_CHARS = 400
DEFAULT_CONFIDENCE = 0.5


@dataclass(frozen=True)
class RetrievedClaim:
    """One claim the reader is allowed to see, with its ranking inputs."""

    claim_id: str
    statement: str
    visibility: Visibility
    status: ClaimStatus
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        """Log-safe claim reference (no statement text)."""

        return {
            "claim_id": self.claim_id,
            "visibility": self.visibility.value,
            "status": self.status.value,
            "confidence": round(self.confidence, 4),
        }


def _confidence(claim: ClaimRecord) -> float:
    try:
        value = float(getattr(claim, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, value))


def _normalized_visibility(claim: ClaimRecord) -> Visibility:
    value = getattr(claim, "visibility", Visibility.ATTEMPT)
    if isinstance(value, Visibility):
        return value
    text = str(value or "").strip().lower()
    for candidate in Visibility:
        if candidate.value == text:
            return candidate
    return Visibility.ATTEMPT


def _normalized_status(claim: ClaimRecord) -> ClaimStatus:
    value = getattr(claim, "status", ClaimStatus.CANDIDATE)
    if isinstance(value, ClaimStatus):
        return value
    text = str(value or "").strip().lower()
    for candidate in ClaimStatus:
        if candidate.value == text:
            return candidate
    return ClaimStatus.CANDIDATE


def _rank(claim: ClaimRecord) -> tuple[int, float, str]:
    return (
        -visibility_rank(_normalized_visibility(claim)),
        -_confidence(claim),
        safe_text(getattr(claim, "claim_id", ""), limit=120),
    )


def retrieve_claims(
    ledger: EvidenceLedger,
    *,
    scope: KnowledgeScope,
    reader_visibility: Visibility = Visibility.GRAPH,
    reader_scope: KnowledgeScope | None = None,
    reader_revision: str = "",
    same_chain: bool = False,
    min_visibility: Visibility = Visibility.GRAPH,
    limit: int = MAX_CLAIMS,
) -> list[RetrievedClaim]:
    """Claims visible to this reader under the gate's read rules, best first.

    Disabled/earlier stages return ``[]`` - a stage switch is a feature gate,
    not a hint. Ledger read failures are swallowed: the read face is
    best-effort like the capture hooks.
    """

    if not rollout.graph_sharing_enabled(getattr(ledger, "stage", None)):
        return []
    try:
        claims = ledger.visible(
            scope=scope,
            reader_visibility=reader_visibility,
            reader_scope=reader_scope,
            reader_revision=reader_revision,
            same_chain=same_chain,
            min_visibility=min_visibility,
            limit=limit,
        )
    except Exception:  # pragma: no cover - defensive: a read must never fail a task
        return []
    ranked = sorted(claims, key=_rank)
    results: list[RetrievedClaim] = []
    for claim in ranked[: max(0, int(limit))]:
        statement = safe_text(getattr(claim, "statement", ""), limit=MAX_STATEMENT_CHARS).strip()
        if not statement:
            continue
        results.append(
            RetrievedClaim(
                claim_id=safe_text(getattr(claim, "claim_id", ""), limit=120),
                statement=statement,
                visibility=_normalized_visibility(claim),
                status=_normalized_status(claim),
                confidence=_confidence(claim),
            )
        )
    return results


def claims_to_context_items(
    claims: Sequence[RetrievedClaim],
    *,
    budget_tokens: int | None = None,
    source: str = RETRIEVAL_SOURCE,
) -> tuple[list[ContextItem], dict[str, Any]]:
    """Pack ranked claims into EVIDENCE items within a token budget.

    Returns ``(items, summary)``; ``summary`` is counts/tokens only. A claim
    that does not fit is skipped, not truncated: a half claim reads as a
    different claim.
    """

    budget = (
        rollout.retrieval_budget_tokens()
        if budget_tokens is None
        else max(128, int(budget_tokens))
    )
    items: list[ContextItem] = []
    used = 0
    available = len(claims)
    for claim in claims:
        content = f"Claim ({claim.visibility.value}): {claim.statement}"
        cost = estimate_tokens(content)
        if used + cost > budget:
            continue
        items.append(
            ContextItem(
                item_id=f"knowledge-claim:{claim.claim_id}",
                tier=Tier.EVIDENCE,
                content=content,
                source=source,
                source_version=claim.status.value,
                trust_class="knowledge_claim",
                # Verified means validated *and* promoted: the gate already
                # checked both, so a reader may treat it as established fact.
                verified=claim.status is ClaimStatus.PROMOTED,
                relevance=claim.confidence,
                stable=False,
                token_cost=cost,
            )
        )
        used += cost
        if used >= budget:
            break
    summary: dict[str, Any] = {
        "claims_available": available,
        "claims_used": len(items),
        "tokens": used,
        "budget_tokens": budget,
        "truncated": len(items) < available,
    }
    return items, summary


def knowledge_context_items(
    ledger: EvidenceLedger,
    *,
    scope: KnowledgeScope,
    reader_visibility: Visibility = Visibility.GRAPH,
    reader_scope: KnowledgeScope | None = None,
    reader_revision: str = "",
    budget_tokens: int | None = None,
    limit: int = MAX_CLAIMS,
    same_chain: bool = False,
) -> tuple[list[ContextItem], dict[str, Any]]:
    """Read + pack graph knowledge in one call (the worker's injection point)."""

    claims = retrieve_claims(
        ledger,
        scope=scope,
        reader_visibility=reader_visibility,
        reader_scope=reader_scope,
        reader_revision=reader_revision,
        same_chain=same_chain,
        limit=limit,
    )
    items, summary = claims_to_context_items(claims, budget_tokens=budget_tokens)
    summary["stage"] = rollout.describe_stage(
        getattr(ledger, "stage", None)
    )["stage"]
    summary["claims_visible"] = len(claims)
    return items, summary
