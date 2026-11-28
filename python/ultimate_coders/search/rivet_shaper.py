"""Rivet: utility-preserving repository context shaping (#29).

Ledger item #29 adapts Rivet (arXiv:2610.34967).  Pure semantic retrieval
suffers from *context utility dilution*: the returned snippets look relevant
but omit the helper definitions, caller usages and configuration constraints
the patch actually needs.  Rivet shapes context in three stages:

1. **Signal grounding** — exact identifiers scored across path/symbol/sigdoc/content.
2. **Utility linking** — expand along dependency, usage and configuration edges.
3. **Budget shaping** — pack by priority and degrade snippet → signature → summary
   instead of dropping evidence outright.

Feature flag: ``UC_RIVET_SHAPER`` (default off).
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "UC_RIVET_SHAPER",
    "Representation",
    "EvidenceItem",
    "ShapedEvidence",
    "RivetResult",
    "RivetShaper",
    "extract_identifiers",
    "rivet_shaper_enabled",
]

UC_RIVET_SHAPER = "UC_RIVET_SHAPER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def rivet_shaper_enabled() -> bool:
    """Return whether Rivet context shaping is enabled."""

    return os.environ.get(UC_RIVET_SHAPER, "").strip().lower() in _TRUTHY


def extract_identifiers(text: str) -> tuple[str, ...]:
    """Extract camelCase / snake_case identifiers from an issue statement."""

    found: list[str] = []
    for match in _IDENTIFIER.findall(text or ""):
        if len(match) < 2:
            continue
        found.append(match)
        for part in _CAMEL.split(match):
            if part and part != match and len(part) >= 2:
                found.append(part)
    return tuple(dict.fromkeys(found))


class Representation(str, Enum):
    SNIPPET = "snippet"
    SIGNATURE = "signature"
    SUMMARY = "summary"


@dataclass(frozen=True)
class EvidenceItem:
    path: str
    symbol: str = ""
    signature: str = ""
    content: str = ""
    dependencies: tuple[str, ...] = ()
    usages: tuple[str, ...] = ()
    configs: tuple[str, ...] = ()
    score: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", str(self.path).strip())
        object.__setattr__(self, "symbol", str(self.symbol).strip())
        object.__setattr__(self, "signature", str(self.signature).strip())
        object.__setattr__(self, "content", str(self.content))
        for name in ("dependencies", "usages", "configs"):
            object.__setattr__(
                self,
                name,
                tuple(str(value).strip() for value in getattr(self, name) if str(value).strip()),
            )
        object.__setattr__(self, "score", float(self.score))

    def key(self) -> tuple[str, str]:
        return (self.path, self.symbol)

    def rendered(self, representation: Representation) -> str:
        header = f"{self.path}::{self.symbol}" if self.symbol else self.path
        if representation is Representation.SNIPPET:
            return f"{header}\n{self.content}".rstrip()
        if representation is Representation.SIGNATURE:
            return f"{header}\n{self.signature}".rstrip()
        summary = self.content.strip().splitlines()[0] if self.content.strip() else ""
        if len(summary) > 160:
            summary = summary[:157].rstrip() + "..."
        return f"{header} — {summary}".rstrip(" —")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "signature": self.signature,
            "dependencies": list(self.dependencies),
            "usages": list(self.usages),
            "configs": list(self.configs),
            "score": round(self.score, 6),
        }


@dataclass(frozen=True)
class ShapedEvidence:
    item: EvidenceItem
    representation: Representation
    text: str
    tokens: int
    score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.item.path,
            "symbol": self.item.symbol,
            "representation": self.representation.value,
            "tokens": self.tokens,
            "score": round(self.score, 6),
        }


@dataclass(frozen=True)
class RivetResult:
    issue: str
    shaped: tuple[ShapedEvidence, ...]
    candidate_count: int
    expanded_count: int
    budget_tokens: int
    total_tokens: int

    @property
    def degraded(self) -> int:
        return sum(
            1
            for evidence in self.shaped
            if evidence.representation is not Representation.SNIPPET
        )

    def render(self) -> str:
        return "\n\n".join(evidence.text for evidence in self.shaped)

    def to_dict(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "candidate_count": self.candidate_count,
            "expanded_count": self.expanded_count,
            "budget_tokens": self.budget_tokens,
            "total_tokens": self.total_tokens,
            "degraded": self.degraded,
            "items": [evidence.to_dict() for evidence in self.shaped],
        }


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(
        token.lower()
        for token in _IDENTIFIER.findall(text or "")
        if len(token) >= 2
    )


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4.0))


class RivetShaper:
    """Three-stage context shaper over a repository evidence pool."""

    DOMAIN_WEIGHTS: Mapping[str, float] = {
        "path": 1.6,
        "symbol": 1.5,
        "sigdoc": 1.2,
        "content": 1.0,
    }

    def __init__(self, budget_tokens: int = 8192, max_candidates: int = 64) -> None:
        if budget_tokens < 1:
            raise ValueError("budget_tokens must be >= 1")
        if max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        self.budget_tokens = budget_tokens
        self.max_candidates = max_candidates

    def signal_grounding(
        self, issue: str, pool: Sequence[EvidenceItem]
    ) -> tuple[EvidenceItem, ...]:
        identifiers = extract_identifiers(issue)
        if not identifiers:
            return tuple(sorted(pool, key=lambda item: item.path)[: self.max_candidates])
        query_terms = {identifier.lower() for identifier in identifiers}
        scored: list[EvidenceItem] = []
        for item in pool:
            domains = {
                "path": item.path,
                "symbol": item.symbol,
                "sigdoc": item.signature,
                "content": item.content,
            }
            total = 0.0
            for domain, text in domains.items():
                terms = _tokens(text)
                if not terms:
                    continue
                hits = sum(1 for term in terms if term in query_terms)
                exact_bonus = 2.0 if item.symbol and item.symbol.lower() in query_terms else 0.0
                total += self.DOMAIN_WEIGHTS[domain] * (hits + exact_bonus)
            if total > 0:
                scored.append(
                    EvidenceItem(
                        path=item.path,
                        symbol=item.symbol,
                        signature=item.signature,
                        content=item.content,
                        dependencies=item.dependencies,
                        usages=item.usages,
                        configs=item.configs,
                        score=total,
                    )
                )
        scored.sort(key=lambda item: (-item.score, item.path, item.symbol))
        return tuple(scored[: self.max_candidates])

    def utility_linking(
        self, seeds: Sequence[EvidenceItem], pool: Sequence[EvidenceItem], *, max_depth: int = 2
    ) -> tuple[EvidenceItem, ...]:
        """Expand along dependency/usage/configuration edges."""

        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        by_path: dict[str, list[EvidenceItem]] = {}
        for item in pool:
            by_path.setdefault(item.path, []).append(item)
        by_symbol = {item.symbol: item for item in pool if item.symbol}
        selected: dict[tuple[str, str], EvidenceItem] = {item.key(): item for item in seeds}
        frontier = list(seeds)
        for _ in range(max_depth):
            next_frontier: list[EvidenceItem] = []
            for item in frontier:
                references = (*item.dependencies, *item.usages, *item.configs)
                for reference in references:
                    candidates = list(by_path.get(reference, ()))
                    if reference in by_symbol:
                        candidates.append(by_symbol[reference])
                    for candidate in candidates:
                        if candidate is None or candidate.key() in selected:
                            continue
                        linked = EvidenceItem(
                            path=candidate.path,
                            symbol=candidate.symbol,
                            signature=candidate.signature,
                            content=candidate.content,
                            dependencies=candidate.dependencies,
                            usages=candidate.usages,
                            configs=candidate.configs,
                            score=max(candidate.score, item.score * 0.6),
                        )
                        selected[linked.key()] = linked
                        next_frontier.append(linked)
            frontier = next_frontier
        return tuple(
            sorted(selected.values(), key=lambda item: (-item.score, item.path, item.symbol))
        )

    def budget_shaping(
        self, items: Sequence[EvidenceItem], budget_tokens: int | None = None
    ) -> tuple[ShapedEvidence, ...]:
        budget = self.budget_tokens if budget_tokens is None else int(budget_tokens)
        if budget < 1:
            raise ValueError("budget_tokens must be >= 1")
        remaining = budget
        shaped: list[ShapedEvidence] = []
        for item in items:
            if remaining <= 0:
                break
            for representation in (
                Representation.SNIPPET,
                Representation.SIGNATURE,
                Representation.SUMMARY,
            ):
                text = item.rendered(representation)
                tokens = _estimate_tokens(text)
                if tokens <= remaining:
                    shaped.append(
                        ShapedEvidence(
                            item=item,
                            representation=representation,
                            text=text,
                            tokens=tokens,
                            score=item.score,
                        )
                    )
                    remaining -= tokens
                    break
            else:
                # Degrade, never drop: clip the summary to the remaining budget.
                text = item.rendered(Representation.SUMMARY)
                clipped = text[: max(1, remaining * 4)]
                tokens = _estimate_tokens(clipped)
                if tokens <= remaining:
                    shaped.append(
                        ShapedEvidence(
                            item=item,
                            representation=Representation.SUMMARY,
                            text=clipped,
                            tokens=tokens,
                            score=item.score,
                        )
                    )
                    remaining -= tokens
        return tuple(shaped)

    def shape(
        self,
        issue: str,
        pool: Sequence[EvidenceItem],
        *,
        budget_tokens: int | None = None,
        max_depth: int = 2,
    ) -> RivetResult:
        budget = self.budget_tokens if budget_tokens is None else int(budget_tokens)
        seeds = self.signal_grounding(issue, pool)
        expanded = self.utility_linking(seeds, pool, max_depth=max_depth)
        shaped = self.budget_shaping(expanded, budget)
        return RivetResult(
            issue=issue,
            shaped=shaped,
            candidate_count=len(pool),
            expanded_count=len(expanded),
            budget_tokens=budget,
            total_tokens=sum(evidence.tokens for evidence in shaped),
        )


def shape_context(
    issue: str,
    pool: Sequence[EvidenceItem],
    *,
    budget_tokens: int = 8192,
    max_depth: int = 2,
) -> RivetResult:
    """One-shot helper used by the worker's search-context builder."""

    return RivetShaper(budget_tokens=budget_tokens).shape(
        issue, pool, max_depth=max_depth
    )
