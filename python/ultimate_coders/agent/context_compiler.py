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

Token accounting defaults to an estimate (``len(text) // 4`` rounded up),
never a tokenizer call: the compiler must stay dependency-free and cheap
enough to run at every subtask start. Budgets are therefore approximate on
purpose -- they bound the prompt at the right order of magnitude, and the
manifest reports the estimate rather than pretending to be exact. A caller
that does have a real tokenizer can register it (``register_token_meter``)
and compile with ``token_meter="name"``; the manifest records which meter
produced the numbers so two attempts are never compared across meters.

Budget *allocation* can follow the routed risk (``routing.RiskLevel``):
under a profile in ``RISK_RESERVED_SHARE`` (today: high-risk work), the
(P0, P2) group -- the task contract and verified evidence -- claims a
reserved share of the budget before state, trace, memory or background may
consume it, so a management task cannot lose its evidence to a long error
log. The reservation is a floor, not a cap: unclaimed tokens flow back, and
every risk outside the table (including the default ``"normal"``) compiles
byte-identically to the unprofiled path.

*Rendering* is split for cache affinity: the policy tier is derived
``stable`` (it does not change between attempts) and emitted first by
``render_parts()``, so a cache-aware serving stack can reuse its prefill.
The stable prefix is an interface plus an observable
(``summary()['stable_tokens']``); reusing it still requires a consumer on
the inference side.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any, Callable

TokenMeter = Callable[[str], int]


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

#: Risk-keyed share of the budget reserved for the (POLICY, EVIDENCE) group
#: before lower tiers may consume it -- a high-risk management task gets a
#: higher guarantee for the contract and for verified evidence, so a large
#: state dump or execution trace cannot crowd them out. Keys follow
#: ``routing.RiskLevel``'s vocabulary; any other risk string (including the
#: default ``"normal"``) reserves nothing and compiles byte-identically to
#: the unprofiled path. The reservation is a floor, not a cap: whatever the
#: group does not use flows back to the remaining tiers.
RISK_RESERVED_SHARE = {"high": 0.5}

#: The group a risk reservation applies to: the task contract (P0) and
#: verified evidence (P2), the two things a management task must not lose.
_RESERVED_TIERS = (Tier.POLICY, Tier.EVIDENCE)

#: Markers that make a Hy-Memory experience worth promoting to P2 evidence on
#: a retry round or a high-risk subtask: when the agent is already failing,
#: the failure chain and the troubleshooting experience outrank ordinary
#: long-term memory (law 3: negative-knowledge / diff-chain leverage).
_TROUBLESHOOTING_HINT = re.compile(
    r"(?:error|exception|failed?|failure|timeout|crash|bug|regression|panic|"
    r"root cause|stack trace|\u62a5\u9519|\u9519\u8bef|\u5931\u8d25|"
    r"\u5f02\u5e38|\u5d29\u6e83|\u8d85\u65f6|\u56de\u5f52)",
    re.IGNORECASE,
)

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
    """Deterministic, dependency-free token estimate.

    The pre-compiler rule was ``len(text) / 4``, which is only right for
    English prose: one CJK character is usually one token (not a quarter of
    one), and code symbols are individually tokenized, so a mixed prompt with
    Chinese error text or a code block was under-budgeted and silently
    overran the model's context. We keep the function deterministic and
    dependency-free but count roughly as a tokenizer does:

    * whitespace is free (merged by most BPE vocabularies);
    * ASCII letters/digits cost a quarter token each (``abcd`` = 1);
    * symbols and non-CJK non-ASCII characters cost half a token each;
    * CJK characters cost a full token each.

    The 4× integer arithmetic avoids float ceilings (same results on every
    platform, no ``math`` import).
    """
    if not text:
        return 1
    quarters = 0
    for char in text:
        if char.isspace():
            continue
        code = ord(char)
        if (
            0x3040 <= code <= 0x30FF  # Hiragana/Katakana
            or 0x3400 <= code <= 0x4DBF  # CJK extension A
            or 0x4E00 <= code <= 0x9FFF  # CJK unified ideographs
            or 0xAC00 <= code <= 0xD7AF  # Hangul syllables
            or 0xF900 <= code <= 0xFAFF  # CJK compatibility
        ):
            quarters += 4
        elif code < 128 and char.isalnum():
            quarters += 1
        else:
            quarters += 2
    return max(1, (quarters + 3) // 4)


#: Name of the built-in heuristic meter; recorded in every manifest summary.
DEFAULT_TOKEN_METER = "heuristic"

_TOKEN_METERS: dict[str, TokenMeter] = {DEFAULT_TOKEN_METER: estimate_tokens}


def register_token_meter(name: str, meter: TokenMeter) -> None:
    """Register a token-counting adapter for ``compile_context(token_meter=...)``.

    The compiler never imports a tokenizer itself; a deployment that has one
    (for example the inference stack's real tokenizer) registers it here.
    Registration is process-global and idempotent for the same name, so a
    worker may call this from its startup path.
    """
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("token meter name must not be empty")
    if not callable(meter):
        raise TypeError("token meter must be callable")
    _TOKEN_METERS[key] = meter


def resolve_token_meter(name: str | None = None) -> tuple[str, TokenMeter]:
    """Return ``(canonical_name, meter)`` or raise for an unknown meter.

    Raising (rather than silently falling back) is deliberate: a deployment
    that asks for its real tokenizer must not quietly get the heuristic and
    then compare budgets that were measured differently.
    """
    key = str(name or DEFAULT_TOKEN_METER).strip().lower()
    try:
        return key, _TOKEN_METERS[key]
    except KeyError as exc:
        raise ValueError(f"unknown context token meter: {name!r}") from exc


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
    """Emit-first flag for cache-affinity. The compiler derives it for the
    policy tier (a contract does not change between attempts); other tiers
    stay volatile unless a caller marks them, so unmarked callers see no
    change. See ``ContextManifest.render_parts``."""
    token_cost: int | None = None

    def tokens(self) -> int:
        if self.token_cost is not None:
            return max(0, int(self.token_cost))
        return estimate_tokens(self.content)

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
    token_meter: str = DEFAULT_TOKEN_METER

    @property
    def total_tokens(self) -> int:
        return sum(item.tokens() for item in self.items)

    @property
    def stable_tokens(self) -> int:
        """Tokens a cache-aware stack could reuse across attempts.

        Only a lower bound on real savings: it assumes the serving side
        actually consumes ``render_parts()``'s prefix, which the sandbox
        agent does not do yet (see docs §6). It is the observable that
        makes the cache-affinity structure worth keeping.
        """
        return sum(item.tokens() for item in self.items if item.stable)

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
            "stable_tokens": self.stable_tokens,
            "risk": self.risk,
            "over_budget": self.over_budget,
            "token_meter": self.token_meter,
            "items_by_tier": tiers,
            # One tier vocabulary: a drop names its tier the way
            # ``items_by_tier`` names the surviving ones. The raw enum value
            # needed a second lookup table to answer "which tier starved".
            "dropped": [
                {"item_id": d.item_id, "tier": _TIER_TITLES[d.tier], "reason": d.reason}
                for d in self.dropped
            ],
        }

    # ── rendering ────────────────────────────────────────────────

    def recovery_hint(self) -> str:
        """One-line, reversible pointer to volatile items dropped by budget.

        Only trace and memory drops get a pointer: a retry or a recall can
        restore those. Policy/state/evidence drops would silently change the
        task or its verified evidence, so the compiler refuses to paper over
        them with a hint (the manifest's ``dropped`` list still records them).
        The line is deterministic (tier, then item id) and bounded, so it does
        not grow with the size of the dropped set.
        """
        recoverable = [
            drop
            for drop in self.dropped
            if drop.reason == "budget" and drop.tier in (Tier.TRACE, Tier.MEMORY)
        ]
        if not recoverable:
            return ""
        recoverable.sort(key=lambda drop: (int(drop.tier), drop.item_id))
        names: list[str] = []
        for tier, label in ((Tier.TRACE, "trace"), (Tier.MEMORY, "memory")):
            ids = list(dict.fromkeys(drop.item_id for drop in recoverable if drop.tier == tier))
            if not ids:
                continue
            shown = ", ".join(ids[:3])
            more = f" +{len(ids) - 3} more" if len(ids) > 3 else ""
            names.append(f"{label}={shown}{more}")
        return (
            "... (recoverable: dropped by budget -- "
            + "; ".join(names)
            + " -- re-run the subtask or recall to restore)"
        )

    def render_parts(self) -> tuple[str, str]:
        """Split the tier-ordered text into (stable prefix, volatile tail).

        Stable items are those whose content does not change between attempts
        (the compiler derives this for the policy tier; other tiers may be
        marked by the caller). They are emitted first so a cache-aware
        serving stack can reuse their prefill. Everything else follows in
        tier order; when the budget dropped trace or memory items, the tail
        ends with a one-line pointer saying they can be restored (the model
        can then ask for them instead of silently acting on a truncated
        prompt). An all-volatile manifest yields an empty prefix; an
        all-stable one an empty tail.
        """
        stable = [i for i in self.items if i.stable]
        volatile = [i for i in self.items if not i.stable]
        tail = _render_items(volatile)
        hint = self.recovery_hint()
        if hint:
            tail = f"{tail}\n{hint}" if tail else hint
        return _render_items(stable), tail

    def render(self) -> str:
        """Tier-ordered rendering with provenance headers."""
        return _render_items(self.items, include_header=True)

    def render_legacy(
        self, *, legacy_style: str = "worker", gateway_truncated: bool = False
    ) -> str:
        """Reproduce the pre-compiler prompt text for one injection path.

        Two callers used to assemble this text independently and therefore
        disagreed about both budget and header. ``legacy_style`` keeps the two
        historical renderings while both now come from one manifest:

        * ``"worker"`` -- dependency results keep the injector's wording,
          order and blank-line rhythm (each line is its own block, exactly as
          ``ContextInjector.build_context`` emits them); the search block
          follows after a blank line, as before. Optional Hy-Memory
          auto-context (P4) appends the same way after evidence.
        * ``"gateway"`` -- the same items rendered under the header the
          gateway-composed block used, with the gateway's own truncation
          marker appended when the upstream block was flagged truncated.

        A budget-truncation marker appears only when the compiler actually
        dropped something, so under a generous budget both styles match their
        old path line for line.
        """
        style = str(legacy_style or "worker").strip().lower()
        if style not in {"worker", "gateway"}:
            raise ValueError(f"unknown legacy context style: {legacy_style!r}")
        deps = [i for i in self.items if i.tier == Tier.TRACE]
        evidence = [i for i in self.items if i.tier == Tier.EVIDENCE]
        memory = [i for i in self.items if i.tier == Tier.MEMORY]
        parts: list[str] = []
        if deps:
            if style == "gateway":
                parts.append("## Context from completed subtasks (gateway-composed)\n")
            else:
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
        for item in memory:
            body = item.content.rstrip("\n")
            block = f"{block}\n\n{body}" if block else body
        if style == "gateway" and not block:
            # Old gateway path returned "" for an empty entry list; keep that
            # exact contract (an empty block means "no context", not a lone
            # truncation notice).
            return ""
        notices: list[str] = []
        if style == "gateway" and gateway_truncated:
            notices.append("... (context truncated by gateway)")
        if any(d.reason == "budget" for d in self.dropped):
            notices.append("... (context truncated by budget)")
        if notices:
            joined = "\n".join(notices)
            block = f"{block}\n{joined}" if block else joined
        return block


def compile_context(
    items: Iterable[ContextItem],
    *,
    budget_tokens: int,
    risk: str = "normal",
    now: float | None = None,
    token_meter: str | None = None,
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
    5. A risk profile in ``RISK_RESERVED_SHARE`` (today: high-risk work)
       reserves a share of the budget for the (P0, P2) group before lower
       tiers may consume it. The reservation is a floor, not a cap: what the
       group does not use flows back, and items deferred past it still
       compete in the ordinary tier order. Unrecognized risks (including
       the default ``"normal"``) reserve nothing.
    6. Cache affinity: the policy tier is marked ``stable`` (a contract
       does not change between attempts), so ``render_parts()`` emits it
       as a reusable prefix. Other tiers stay volatile unless the caller
       marks them -- an unmarked caller sees no ordering or rendering
       change.

    Token costs are measured once, up front, by ``token_meter`` (default
    ``"heuristic"``); items that already carry ``token_cost`` keep it. The
    chosen meter's name is recorded on the manifest.
    """
    now = time.time() if now is None else now
    meter_name, meter = resolve_token_meter(token_meter)
    manifest = ContextManifest(
        budget_tokens=budget_tokens, risk=risk, token_meter=meter_name
    )

    # Cache affinity, derived not configured: the policy tier (task
    # contract) does not change between attempts of one compile, so it is
    # the natural stable prefix for a cache-aware serving stack. Every
    # other tier keeps whatever the caller marked -- an unmarked item
    # stays volatile, so callers that never heard of ``stable`` compile
    # exactly as before (the policy tier already sorts first, so the
    # packing order is unchanged too).
    normalized: list[ContextItem] = []
    for item in items:
        updates: dict[str, Any] = {}
        if item.tier == Tier.POLICY and not item.stable:
            updates["stable"] = True
        if item.token_cost is None:
            updates["token_cost"] = max(1, int(meter(item.content)))
        elif item.token_cost < 0:
            updates["token_cost"] = 0
        normalized.append(replace(item, **updates) if updates else item)
    items = normalized

    deduped: dict[str, ContextItem] = {}
    for item in items:
        if item.expired(now):
            manifest.dropped.append(DroppedItem(item.item_id, item.tier, "expired"))
            continue
        if item.content.strip():
            key = _content_key(item.content)
        else:
            key = f"empty:{int(item.tier)}:{item.item_id}"
        incumbent = deduped.get(key)
        if incumbent is None or _preference(item) < _preference(incumbent):
            if incumbent is not None:
                manifest.dropped.append(DroppedItem(incumbent.item_id, incumbent.tier, "duplicate"))
            deduped[key] = item
        else:
            manifest.dropped.append(DroppedItem(item.item_id, item.tier, "duplicate"))

    ordered = sorted(deduped.values(), key=_packing_key)
    reserved = int(budget_tokens * RISK_RESERVED_SHARE.get(risk, 0.0))
    if reserved > 0:
        # High-risk pass: the (P0, P2) group claims the reserved share first
        # (P0 protected as always), so a large state dump or execution trace
        # cannot crowd out the contract or verified evidence.
        group = [i for i in ordered if i.tier in _RESERVED_TIERS]
        rest = [i for i in ordered if i.tier not in _RESERVED_TIERS]
        deferred = _pack_first_fit(group, reserved, manifest)
        # Whatever the group left unclaimed flows back; the remaining tiers
        # (deferred evidence included) then pack in the ordinary tier order.
        leftover = budget_tokens - manifest.total_tokens
        remainder = sorted(deferred + rest, key=_packing_key)
    else:
        remainder = ordered
        leftover = budget_tokens
    for item in _pack_first_fit(remainder, leftover, manifest):
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


def from_gateway_context_block(
    block: Any, *, source: str = "gateway-context"
) -> list[ContextItem]:
    """Wrap a gateway-composed context block as P3 trace items.

    The gateway envelope carries already-committed subtask outputs as
    ``{"entries": [{"node_id", "success", "summary"}, ...], "truncated": bool}``.
    Before unification the worker string-concatenated a rendering of this
    block outside the budget; this adapter makes the same facts ordinary
    context items so the one compiler measures and bounds them like every
    other source. Empty/missing block yields no items (the carried block is
    still authoritative: no block means the caller must not fall back to the
    local injector; that decision stays in the worker).
    """
    data = block if isinstance(block, dict) else {}
    entries = data.get("entries") or []
    items: list[ContextItem] = []
    for entry in entries:
        if isinstance(entry, dict):
            node_id = str(entry.get("node_id") or "")
            success = bool(entry.get("success"))
            summary = str(entry.get("summary") or "")
        else:  # tolerate attribute-style payloads (envelope proto types)
            node_id = str(getattr(entry, "node_id", "") or "")
            success = bool(getattr(entry, "success", False))
            summary = str(getattr(entry, "summary", "") or "")
        content = f"Summary: {summary[:1000]}" if summary else ""
        items.append(
            ContextItem(
                item_id=node_id,
                tier=Tier.TRACE,
                content=content,
                source=source,
                source_version="ok" if success else "failed",
                trust_class="gateway_committed",
                verified=success,
                relevance=1.0 if success else 0.5,
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


def from_hy_memory_block(text: str | None, *, source: str = "hy-memory") -> list[ContextItem]:
    """Wrap an optional Hy-Memory auto-context block as a single P4 memory item.

    Soft-skip: ``None`` / empty text yields no items so the worker can treat a
    missing package or failed recall as a no-op without claiming inject succeeded.
    """
    if not text:
        return []
    return [
        ContextItem(
            item_id="hy-memory-block",
            tier=Tier.MEMORY,
            content=text,
            source=source,
            trust_class="hy_memory_recall",
            verified=False,
            relevance=0.6,
        )
    ]


def from_hy_memory_channels(
    channels: Any,
    *,
    promote_negative: bool = False,
    source: str = "hy-memory",
) -> list[ContextItem]:
    """Split a Hy-Memory recall into per-channel items with stable ids.

    ``channels`` is the normalised ``CodingMemory.recall`` payload (``facts`` /
    ``experiences`` / ``chain_block``). Each channel becomes its own item so
    the compiler can budget and revive them independently instead of treating
    one big block as all-or-nothing: a long chain can be dropped while the two
    short facts survive, and a later revision can bring back only the missing
    channel.

    ``promote_negative`` is the retry/high-risk switch: when the worker is on
    attempt >= 2 or the routed risk reserves evidence, the version chain and
    troubleshooting experiences move from P4 memory to P2 evidence. That is
    what the high-risk reservation exists for -- the failure chain is evidence,
    not background, once the agent is already failing.

    Superseded facts must already be filtered by the recall path; this adapter
    does not re-interpret metadata.
    """
    if not isinstance(channels, dict):
        return []
    items: list[ContextItem] = []

    for index, fact in enumerate(channels.get("facts") or []):
        if isinstance(fact, dict):
            statement = fact.get("statement") or fact.get("content") or ""
        else:
            statement = str(fact)
        text = str(statement).strip()
        if not text:
            continue
        items.append(
            ContextItem(
                item_id=f"hy-memory-fact-{index}",
                tier=Tier.MEMORY,
                content=f"- [fact] {text}",
                source=source,
                trust_class="hy_memory_recall",
                relevance=0.6,
            )
        )

    for index, experience in enumerate(channels.get("experiences") or []):
        if isinstance(experience, dict):
            task = str(experience.get("task") or experience.get("content") or "").strip()
            solution = str(experience.get("solution") or "").strip()
        else:
            task, solution = str(experience).strip(), ""
        if not task:
            continue
        troubleshooting = bool(_TROUBLESHOOTING_HINT.search(f"{task} {solution}"))
        line = f"- [experience] {task}" + (f" -> {solution}" if solution else "")
        items.append(
            ContextItem(
                item_id=f"hy-memory-experience-{index}",
                tier=Tier.EVIDENCE if promote_negative and troubleshooting else Tier.MEMORY,
                content=line,
                source=source,
                trust_class="hy_memory_recall",
                relevance=0.75 if troubleshooting else 0.6,
            )
        )

    chain_block = channels.get("chain_block")
    chain_items: list[str] = []
    if chain_block:
        if isinstance(chain_block, dict):
            chain_text = chain_block.get("text") or chain_block.get("summary") or ""
        else:
            chain_text = str(chain_block)
        text = str(chain_text).strip()
        if text:
            chain_items.append(f"- [chain] {text}")
    for index, line in enumerate(chain_items):
        items.append(
            ContextItem(
                item_id=f"hy-memory-chain-{index}",
                tier=Tier.EVIDENCE if promote_negative else Tier.MEMORY,
                content=line,
                source=source,
                trust_class="hy_memory_chain",
                relevance=0.8,
            )
        )
    return items


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


def _pack_first_fit(
    items: list[ContextItem], remaining: int, manifest: ContextManifest
) -> list[ContextItem]:
    """Append every item that fits ``remaining``; return the ones that did not.

    Protected tiers are appended regardless (the caller reports
    ``over_budget`` afterwards); ``remaining`` may go negative on their
    account, which simply excludes everything after them.
    """
    not_fit: list[ContextItem] = []
    for item in items:
        cost = item.tokens()
        if item.tier in PROTECTED_TIERS:
            manifest.items.append(item)
            remaining -= cost
        elif cost <= remaining:
            manifest.items.append(item)
            remaining -= cost
        else:
            not_fit.append(item)
    return not_fit


def _render_items(items: list[ContextItem], *, include_header: bool = False) -> str:
    if not items:
        return ""
    ordered = sorted(items, key=lambda item: int(item.tier)) if include_header else items
    parts: list[str] = []
    if include_header:
        parts.append("## Compiled context\n")
    current: Tier | None = None
    for item in ordered:
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
