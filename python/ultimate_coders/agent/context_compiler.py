"""Deterministic context compilation for one subtask prompt.

The worker assembles a prompt from several sources -- completed dependency
results, cross-repo search hits, and (in the gateway path) an envelope-block
composed upstream. Historically each source truncated itself independently
(a fixed character cap per summary, plus a whole-block cap), which made the
total unbounded in principle and invisible in practice: nothing recorded what
was dropped, where a fact came from, or how stale it was.

This module makes the assembly explicit. A ``ContextManifest`` is compiled
from ``ContextItem`` values whose tier (P0 policy .. P5 background) follows
the design in ``docs/agent-os-architecture.md``: policy and live state are
never sacrificed to fit a budget, evidence outranks trace, trace outranks
long-term memory, and background only fills what is left. Compilation is
deterministic -- the same inputs produce byte-identical output -- so the
manifest can be asserted in tests and diffed in events.

Rendering stays compatible with the pre-existing prompt text: for the
dependency and search sources ``render_legacy()`` reproduces exactly what the
worker emitted before, and only adds a truncation marker when the budget
actually forced a drop. The tier-ordered ``render()`` is the shape new
sources should target.

Token accounting is an estimate (``len(text) // 4`` rounded up), never a
tokenizer call: the compiler must stay dependency-free and cheap enough to
run at every subtask start. Budgets are therefore approximate on purpose --
they bound the prompt at the right order of magnitude, and the manifest
reports the estimate rather than pretending to be exact.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Tier(IntEnum):
    """Context priority tiers, highest first (see the architecture doc)."""

    POLICY = 0  # task contract, constraints -- must never be dropped
    STATE = 1  # current step, latest errors -- must stay fresh
    EVIDENCE = 2  # verified facts, artifacts, search hits -- by trust
    TRACE = 3  # recent execution results -- compressed, causal
    MEMORY = 4  # long-term memory / learned skills -- dedup, TTL
    BACKGROUND = 5  # general background -- only fills leftover budget


#: Tiers that are never dropped for budget reasons. P0 is the contract the
#: task is judged against; P1 is where the task currently is. Dropping either
#: silently changes what the agent is asked to do (or what it knows it has
#: already done), which is worse than exceeding the budget and saying so.
PROTECTED_TIERS = (Tier.POLICY,)

#: Section titles for the tier-ordered renderer.
_TIER_TITLES = {
    Tier.POLICY: "P0 policy",
    Tier.STATE: "P1 state",
    Tier.EVIDENCE: "P2 evidence",
    Tier.TRACE: "P3 trace",
    Tier.MEMORY: "P4 memory",
    Tier.BACKGROUND: "P5 background",
}


def estimate_tokens(text: str) -> int:
    """Deterministic, dependency-free token estimate (4 characters/token)."""
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class ContextItem:
    """One compilable unit of context, carrying its own provenance.

    ``verified`` may only be set when some *specific* check passed (a file
    version was validated, a result was confirmed). It never means "the
    content is true" -- a highly relevant external page can still carry a
    prompt injection, which is why trust is a separate axis from relevance
    and why the tool gateway validates permissions independently.
    """

    item_id: str
    tier: Tier
    content: str
    source: str = ""
    source_version: str = ""
    observed_at: float | None = None
    trust_class: str = "unverified"
    verified: bool = False
    ttl_seconds: float | None = None
    relevance: float = 0.0
    stable: bool = False
    token_cost: int | None = None

    def tokens(self) -> int:
        return self.token_cost if self.token_cost is not None else estimate_tokens(self.content)

    def expired(self, now: float) -> bool:
        if self.ttl_seconds is None or self.observed_at is None:
            return False
        return self.observed_at + self.ttl_seconds <= now


@dataclass(frozen=True)
class DroppedItem:
    """Why an item did not make it into the manifest."""

    item_id: str
    tier: Tier
    reason: str  # "expired" | "duplicate" | "budget"


@dataclass
class ContextManifest:
    """The compiled result: included items, drops, and cost accounting."""

    items: list[ContextItem] = field(default_factory=list)
    dropped: list[DroppedItem] = field(default_factory=list)
    budget_tokens: int = 0
    risk: str = "normal"
    over_budget: bool = False

    @property
    def total_tokens(self) -> int:
        return sum(item.tokens() for item in self.items)

    def by_tier(self) -> dict[Tier, list[ContextItem]]:
        grouped: dict[Tier, list[ContextItem]] = {}
        for item in self.items:
            grouped.setdefault(item.tier, []).append(item)
        return grouped

    def summary(self) -> dict[str, Any]:
        """Compact, log-safe description (no content) for events/traces."""
        tiers: dict[str, int] = {}
        for item in self.items:
            key = _TIER_TITLES[item.tier]
            tiers[key] = tiers.get(key, 0) + 1
        return {
            "budget_tokens": self.budget_tokens,
            "total_tokens": self.total_tokens,
            "risk": self.risk,
            "over_budget": self.over_budget,
            "items_by_tier": tiers,
            "dropped": [
                {"item_id": d.item_id, "tier": int(d.tier), "reason": d.reason}
                for d in self.dropped
            ],
        }

    # ── rendering ────────────────────────────────────────────────

    def render_parts(self) -> tuple[str, str]:
        """Split the tier-ordered text into (stable prefix, volatile tail).

        Stable items are those whose content does not change between attempts
        (policy, verified evidence); they are emitted first so a cache-aware
        serving stack can reuse their prefill. Everything else follows in
        tier order.
        """
        stable = [i for i in self.items if i.stable]
        volatile = [i for i in self.items if not i.stable]
        return _render_items(stable), _render_items(volatile)

    def render(self) -> str:
        """Tier-ordered rendering with provenance headers."""
        return _render_items(self.items, include_header=True)

    def render_legacy(self) -> str:
        """Reproduce the pre-compiler worker prompt text.

        Dependency results keep the injector's wording, order and blank-line
        rhythm (each line is its own block, exactly as
        ``ContextInjector.build_context`` emits them); the search block
        follows after a blank line, as before. A truncation marker appears
        only when the budget dropped something -- under a generous budget the
        output matches the old path line for line.
        """
        deps = [i for i in self.items if i.tier == Tier.TRACE]
        evidence = [i for i in self.items if i.tier == Tier.EVIDENCE]
        parts: list[str] = []
        if deps:
            parts.append("## Context from completed subtasks\n")
            parts.append("The following subtasks have completed. Use their results as context:\n")
            for item in deps:
                node_id = str(item.item_id)
                success = item.source_version != "failed"
                parts.append(f"### Subtask {node_id[:8]} ({'✓' if success else '✗'})\n")
                for line in item.content.splitlines():
                    if line:
                        parts.append(f"{line}\n")
                parts.append("")
            block = "\n".join(parts)
        else:
            block = ""
        for item in evidence:
            body = item.content.rstrip("\n")
            block = f"{block}\n\n{body}" if block else body
        if any(d.reason == "budget" for d in self.dropped):
            marker = "... (context truncated by budget)"
            block = f"{block}\n{marker}" if block else marker
        return block


def compile_context(
    items: Iterable[ContextItem],
    *,
    budget_tokens: int,
    risk: str = "normal",
    now: float | None = None,
) -> ContextManifest:
    """Select and order items under a token budget, deterministically.

    Rules, in order:

    1. Expired items (``ttl_seconds`` relative to ``now``) are dropped.
    2. Exact-content duplicates keep the most trusted copy: lower tier wins,
       then ``verified``, then ``stable``, then relevance, then item id.
    3. Items are then packed by tier (P0 first). P0 is never dropped; if it
       alone exceeds the budget the manifest reports ``over_budget`` instead
       of silently changing the task contract. Every later item is included
       only if it fits what is left, so a small item may still fit after a
       large one in the same tier was skipped.
    4. Ties inside a tier order by ``verified`` desc, ``relevance`` desc,
       ``item_id`` asc -- never by input order, so reordering the caller's
       input cannot change the prompt.
    """
    now = time.time() if now is None else now
    manifest = ContextManifest(budget_tokens=budget_tokens, risk=risk)

    deduped: dict[str, ContextItem] = {}
    for item in items:
        if item.expired(now):
            manifest.dropped.append(DroppedItem(item.item_id, item.tier, "expired"))
            continue
        key = _content_key(item.content)
        incumbent = deduped.get(key)
        if incumbent is None or _preference(item) < _preference(incumbent):
            if incumbent is not None:
                manifest.dropped.append(DroppedItem(incumbent.item_id, incumbent.tier, "duplicate"))
            deduped[key] = item
        else:
            manifest.dropped.append(DroppedItem(item.item_id, item.tier, "duplicate"))

    ordered = sorted(deduped.values(), key=_packing_key)
    remaining = budget_tokens
    for item in ordered:
        cost = item.tokens()
        if item.tier in PROTECTED_TIERS:
            manifest.items.append(item)
            remaining -= cost
            continue
        if cost <= remaining:
            manifest.items.append(item)
            remaining -= cost
        else:
            manifest.dropped.append(DroppedItem(item.item_id, item.tier, "budget"))

    if manifest.total_tokens > budget_tokens:
        manifest.over_budget = True
    return manifest


# ── adapters for the worker's existing sources ──────────────────────


def from_dependency_results(results: Iterable[Any]) -> list[ContextItem]:
    """Turn ``SubtaskContext`` values into P3 trace items.

    Kept duck-typed (``subtask_id``/``summary``/``modified_files``/
    ``key_findings``/``success``) so this module does not import the agent
    package and stays trivially testable.
    """
    items: list[ContextItem] = []
    for ctx in results:
        lines: list[str] = []
        if ctx.summary:
            lines.append(f"Summary: {ctx.summary[:1000]}")
        if ctx.modified_files:
            lines.append(f"Modified files: {', '.join(ctx.modified_files[:10])}")
        for finding in (ctx.key_findings or [])[:5]:
            lines.append(f"- {finding[:200]}")
        items.append(
            ContextItem(
                item_id=str(ctx.subtask_id),
                tier=Tier.TRACE,
                content="\n".join(lines),
                source="subtask",
                source_version="ok" if ctx.success else "failed",
                trust_class="worker_reported",
                verified=bool(ctx.success),
                relevance=1.0 if ctx.success else 0.5,
            )
        )
    return items


def from_search_block(text: str | None, *, source: str = "repo-search") -> list[ContextItem]:
    """Wrap the cross-repo search block as a single P2 evidence item."""
    if not text:
        return []
    return [
        ContextItem(
            item_id="search-block",
            tier=Tier.EVIDENCE,
            content=text,
            source=source,
            trust_class="repository_search",
            verified=False,
            relevance=0.8,
        )
    ]


# ── internals ───────────────────────────────────────────────────────


def _content_key(content: str) -> str:
    return hashlib.sha1(content.strip().encode("utf-8")).hexdigest()


def _preference(item: ContextItem) -> tuple:
    """Lower tuple = preferred when the same content appears twice."""
    return (
        int(item.tier),
        0 if item.verified else 1,
        0 if item.stable else 1,
        -item.relevance,
        item.item_id,
    )


def _packing_key(item: ContextItem) -> tuple:
    return (
        int(item.tier),
        0 if item.verified else 1,
        -item.relevance,
        0 if item.stable else 1,
        item.item_id,
    )


def _render_items(items: list[ContextItem], *, include_header: bool = False) -> str:
    if not items:
        return ""
    parts: list[str] = []
    if include_header:
        parts.append("## Compiled context\n")
    current: Tier | None = None
    for item in items:
        if item.tier != current:
            current = item.tier
            if include_header:
                parts.append(f"\n### {_TIER_TITLES[item.tier]}\n")
        if include_header:
            origin = item.source or "unknown"
            flags = []
            if item.verified:
                flags.append("verified")
            if item.source_version:
                flags.append(f"v={item.source_version}")
            if item.observed_at is not None:
                flags.append(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(item.observed_at)))
            suffix = f" ({', '.join(flags)})" if flags else ""
            parts.append(f"{origin}{suffix}: ")
        parts.append(item.content)
        if not item.content.endswith("\n"):
            parts.append("\n")
    return "".join(parts)
