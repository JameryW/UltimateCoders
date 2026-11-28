"""Acceptance-criteria audit for a finished attempt (#89), production wiring.

SWE-CoWork (arXiv:2611.20218) measured the gap this adapter closes: agents pass
the F2P tests about 63% of the time yet deliver the complete requested change
less than 8% of the time, because nobody re-checks the requirement list against
what the patch actually did. ``requirement_tracker.py`` holds the ledger; this
module turns the subtask description's acceptance bullets into ledger entries
and marks each one verified or failed against the evidence the worker already
holds (patch diff, executed commands, bounded test output).

Enable with ``UC_REQUIREMENT_AUDIT=1`` (``UC_REQUIREMENT_TRACKER`` must be on
too). The audit is advisory by design: a description bullet list is not a
machine-checkable contract, so the worker reports coverage and never rejects a
subtask on it. Pure bookkeeping: deterministic, bounded, no model or network.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ultimate_coders.agent.requirement_tracker import (
    Requirement,
    RequirementTracker,
    requirement_tracker_enabled,
)

__all__ = [
    "MAX_REQUIREMENTS",
    "REQUIREMENT_AUDIT_ENV",
    "RequirementAudit",
    "audit_requirements",
    "extract_requirements",
    "keywords",
    "requirement_audit_enabled",
]

REQUIREMENT_AUDIT_ENV = "UC_REQUIREMENT_AUDIT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MAX_REQUIREMENTS = 8
_MAX_KEYWORDS = 6
_MIN_KEYWORD_LEN = 4

# A bullet is prose, not a predicate: "invalidate the cache on write" is
# satisfied by a patch that invalidates the cache even though the conditional
# wording never appears verbatim. Requiring a majority of the distinctive words
# keeps the check honest without demanding a literal sentence match, while a
# requirement whose distinctive words are absent still fails (0.0 < 0.6).
_MATCH_RATIO = 0.6

_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.+?)\s*$")
_WORD = re.compile(r"[a-z0-9_]+")

# Only the shortest glue words are dropped; domain words such as "add" or
# "return" are deliberately kept because they carry the requested behaviour.
_STOPWORDS = frozenset(
    {
        "also",
        "and",
        "are",
        "but",
        "can",
        "for",
        "from",
        "has",
        "have",
        "into",
        "its",
        "must",
        "not",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "this",
        "when",
        "where",
        "which",
        "will",
        "with",
        "would",
        "your",
    }
)


def requirement_audit_enabled() -> bool:
    """True when the audit flag and the underlying tracker flag are both on."""

    if os.environ.get(REQUIREMENT_AUDIT_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return requirement_tracker_enabled()


def _normalize(text: str) -> str:
    """Strip markdown emphasis and backticks from one bullet's text."""

    cleaned = text.replace("`", "").replace("**", "").replace("__", "")
    return " ".join(cleaned.split())


def extract_requirements(
    description: str, *, max_items: int = MAX_REQUIREMENTS
) -> tuple[Requirement, ...]:
    """Turn acceptance bullets in ``description`` into ledger entries.

    Only lines that open with a bullet or ordinal marker count as criteria;
    prose paragraphs are ignored rather than guessed at. Duplicates are dropped
    case-insensitively and the result is capped at ``max_items``.
    """

    if max_items <= 0:
        raise ValueError("max_items must be positive")
    seen: set[str] = set()
    requirements: list[Requirement] = []
    for line in (description or "").splitlines():
        match = _BULLET.match(line)
        if match is None:
            continue
        text = _normalize(match.group(1))
        if len(text) < 3:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        requirements.append(Requirement(f"R{len(requirements) + 1}", text))
        if len(requirements) >= max_items:
            break
    return tuple(requirements)


def keywords(text: str) -> tuple[str, ...]:
    """The distinctive words of a requirement, in first-appearance order."""

    seen: list[str] = []
    for word in _WORD.findall(text.lower()):
        if len(word) < _MIN_KEYWORD_LEN or word in _STOPWORDS:
            continue
        if word in seen:
            continue
        seen.append(word)
        if len(seen) >= _MAX_KEYWORDS:
            break
    return tuple(seen)


@dataclass(frozen=True)
class RequirementAudit:
    """Coverage of one attempt against the description's acceptance bullets."""

    requirements: tuple[Requirement, ...]
    verified: tuple[str, ...]
    unverified: tuple[str, ...]
    coverage: float
    complete: bool

    @property
    def unverified_requirements(self) -> tuple[Requirement, ...]:
        wanted = set(self.unverified)
        return tuple(item for item in self.requirements if item.requirement_id in wanted)

    def to_dict(self) -> dict[str, object]:
        return {
            "total": len(self.requirements),
            "verified": len(self.verified),
            "coverage": round(self.coverage, 3),
            "complete": self.complete,
            "unverified": [item.text for item in self.unverified_requirements],
            "requirements": [item.to_dict() for item in self.requirements],
        }


def audit_requirements(
    description: str,
    *,
    patch_text: str = "",
    commands_run: tuple[str, ...] | list[str] = (),
    test_output: str = "",
    max_items: int = MAX_REQUIREMENTS,
) -> RequirementAudit | None:
    """Mark each acceptance bullet verified when its keywords appear in evidence.

    ``None`` means the description carried no bullets, so there is nothing to
    audit and the caller should stay silent rather than report 0/0 coverage.
    """

    requirements = extract_requirements(description, max_items=max_items)
    if not requirements:
        return None
    evidence = "\n".join(
        [patch_text or "", test_output or "", *(str(item) for item in commands_run)]
    ).lower()
    tracker = RequirementTracker()
    tracked: dict[str, Requirement] = {}
    verified: list[str] = []
    unverified: list[str] = []
    for requirement in requirements:
        tracked[requirement.requirement_id] = tracker.add(
            requirement.requirement_id, requirement.text
        )
        terms = keywords(requirement.text)
        matched = tuple(term for term in terms if term in evidence)
        covered = bool(terms) and len(matched) / len(terms) >= _MATCH_RATIO
        if covered:
            tracker.verify(
                requirement.requirement_id,
                "keywords observed in patch/tests: " + ", ".join(matched),
            )
            verified.append(requirement.requirement_id)
        else:
            tracker.fail(requirement.requirement_id)
            unverified.append(requirement.requirement_id)
    total = len(requirements)
    return RequirementAudit(
        requirements=tuple(tracked[item.requirement_id] for item in requirements),
        verified=tuple(verified),
        unverified=tuple(unverified),
        coverage=len(verified) / total,
        complete=not unverified,
    )
