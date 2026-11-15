"""MemCredit marginal-utility retrieval arbitration and directory trie.

The retrieval face of an attempt can receive candidates from several stages
(plain text files, AST symbols, Qdrant hits). Sending every candidate wastes
the attempt token budget, and overlapping fragments repeat the same evidence
twice. :func:`select_memcredit` implements a small greedy marginal-utility
arbiter:

* a candidate's density is ``(max(0, score) + 0.25 * stage bonus) / tokens``
  (multiplied by the subtree weight when a directory sibling was already
  selected);
* candidates whose word tokens overlap an already selected candidate are
  skipped;
* a candidate never pushes the selection over the token budget.

Attempt one gets :data:`ATTEMPT_ONE_BUDGET` tokens; every retry gets
:data:`RETRY_BUDGET`. The module is dependency free (no OpenViking import) so
the arbiter can be unit tested and shared by the worker and the orchestrator.
Feature flag: ``UC_MEMCREDIT``.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ATTEMPT_ONE_BUDGET",
    "MEMCREDIT_ENABLED_ENV",
    "RETRY_BUDGET",
    "DirectoryTrie",
    "MemCreditCandidate",
    "MemCreditResult",
    "SkippedCandidate",
    "budget_for_attempt",
    "directory_affinity",
    "estimate_tokens",
    "memcredit_enabled",
    "select_memcredit",
    "token_jaccard",
]

ATTEMPT_ONE_BUDGET = 800
RETRY_BUDGET = 3000

MEMCREDIT_ENABLED_ENV = "UC_MEMCREDIT"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_STAGE_BONUS = {"ast": 0.15, "qdrant": 0.10, "text": 0.0}
_STAGE_WEIGHT = 0.25
_SUBTREE_AFFINITY = 1.5
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_SKIP_REASONS = frozenset({"budget", "overlap", "duplicate", "empty"})
_CANDIDATE_FIELDS = frozenset(
    {
        "identifier",
        "content",
        "path",
        "tokens",
        "score",
        "stage",
        "symbol",
        "metadata",
    }
)


def _text(value: Any) -> str:
    """Coerce ``value`` into a string without ever raising."""

    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Best-effort finite float coercion; malformed input falls back."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def budget_for_attempt(attempt: int) -> int:
    """Token budget for one attempt (attempt 0 = first try)."""

    try:
        index = int(attempt)
    except (TypeError, ValueError):
        index = 0
    return ATTEMPT_ONE_BUDGET if index <= 0 else RETRY_BUDGET


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~four characters per token), never below one."""

    size = len(text) if isinstance(text, str) else 0
    return max(1, (size + 3) // 4)


@dataclass(frozen=True)
class MemCreditCandidate:
    """One retrieval candidate considered by the arbiter."""

    identifier: str
    content: str
    path: str = ""
    tokens: int = 0
    score: float = 0.0
    stage: str = "text"
    symbol: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", _text(self.identifier))
        object.__setattr__(self, "content", _text(self.content))
        object.__setattr__(self, "path", _text(self.path))
        object.__setattr__(self, "symbol", _text(self.symbol))
        object.__setattr__(self, "stage", _text(self.stage).strip() or "text")
        try:
            tokens = int(self.tokens)
        except (TypeError, ValueError):
            tokens = 0
        if tokens <= 0:
            tokens = estimate_tokens(self.content)
        object.__setattr__(self, "tokens", tokens)
        object.__setattr__(self, "score", _finite_float(self.score))
        metadata = self.metadata if isinstance(self.metadata, Mapping) else {}
        object.__setattr__(self, "metadata", dict(metadata))


@dataclass(frozen=True)
class SkippedCandidate:
    """One candidate that did not make the selection, with the reason."""

    identifier: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", _text(self.identifier))
        reason = _text(self.reason).strip().lower()
        object.__setattr__(self, "reason", reason if reason in _SKIP_REASONS else "empty")


@dataclass(frozen=True)
class MemCreditResult:
    """Outcome of :func:`select_memcredit`."""

    selected: tuple[MemCreditCandidate, ...]
    skipped: tuple[SkippedCandidate, ...]
    tokens_used: int
    budget: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected", tuple(self.selected or ()))
        object.__setattr__(self, "skipped", tuple(self.skipped or ()))
        try:
            tokens_used = int(self.tokens_used)
        except (TypeError, ValueError):
            tokens_used = 0
        object.__setattr__(self, "tokens_used", max(0, tokens_used))
        try:
            budget = int(self.budget)
        except (TypeError, ValueError):
            budget = 0
        object.__setattr__(self, "budget", max(0, budget))


def _word_tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN_PATTERN.finditer(_text(text))}


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity of the lowercased word tokens of two payloads."""

    left = _word_tokens(a)
    right = _word_tokens(b)
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union)


def _normalise_path(value: str) -> str:
    text = _text(value).strip().replace("\\", "/")
    segments = [segment for segment in text.split("/") if segment not in ("", ".")]
    return "/".join(segments)


def _looks_like_directory(value: str) -> bool:
    return _text(value).strip().replace("\\", "/").endswith("/")


def _parent_segments(path: str) -> tuple[str, ...]:
    """Ancestor directory segments of ``path`` (empty for a root-level file)."""

    normalised = _normalise_path(path)
    if not normalised:
        return ()
    segments = normalised.split("/")
    if _looks_like_directory(path):
        return tuple(segments)
    return tuple(segments[:-1])


def directory_affinity(path_a: str, path_b: str) -> float:
    """Return 1.5 when two paths share at least one parent directory, else 1.0.

    Every ancestor directory counts, so two files in sibling sub-directories
    of the same package score 1.5. Root-level files have no parent directory
    and therefore score 1.0. An empty path has no affinity.
    """

    normalised_a = _normalise_path(path_a)
    normalised_b = _normalise_path(path_b)
    if not normalised_a or not normalised_b:
        return 1.0
    parents_a = set(_parent_segments(path_a))
    parents_b = set(_parent_segments(path_b))
    if parents_a and parents_b and parents_a & parents_b:
        return _SUBTREE_AFFINITY
    return 1.0


def _stage_bonus(stage: str) -> float:
    return _STAGE_BONUS.get(_text(stage).strip().lower(), 0.0)


def _coerce_candidate(item: Any) -> MemCreditCandidate | None:
    if isinstance(item, MemCreditCandidate):
        return item
    if not isinstance(item, Mapping):
        return None
    data = {key: value for key, value in item.items() if key in _CANDIDATE_FIELDS}
    try:
        return MemCreditCandidate(**data)
    except TypeError:
        return None


def _coerce_candidates(candidates: Iterable[Any] | None) -> list[MemCreditCandidate]:
    if candidates is None:
        return []
    try:
        iterator = iter(candidates)
    except TypeError:
        return []
    result: list[MemCreditCandidate] = []
    for item in iterator:
        candidate = _coerce_candidate(item)
        if candidate is not None:
            result.append(candidate)
    return result


def _resolve_budget(budget_tokens: int | None, attempt: int) -> int:
    if budget_tokens is None:
        return budget_for_attempt(attempt)
    try:
        value = int(budget_tokens)
    except (TypeError, ValueError):
        return budget_for_attempt(attempt)
    return max(0, value)


def _density(
    candidate: MemCreditCandidate,
    selected: list[MemCreditCandidate],
    subtree_weight: float,
) -> float:
    contribution = max(0.0, candidate.score)
    contribution += _STAGE_WEIGHT * _stage_bonus(candidate.stage)
    if selected and any(
        directory_affinity(candidate.path, picked.path) >= _SUBTREE_AFFINITY
        for picked in selected
    ):
        contribution *= subtree_weight
    return contribution / max(1, candidate.tokens)


def select_memcredit(
    candidates: Iterable[MemCreditCandidate | Mapping[str, Any]],
    budget_tokens: int | None = None,
    *,
    attempt: int = 0,
    overlap_threshold: float = 0.8,
    subtree_weight: float = 1.5,
) -> MemCreditResult:
    """Greedily select retrieval candidates by marginal utility per token.

    ``selected`` is the pick order. ``skipped`` lists every rejected candidate
    with one of ``empty``/``duplicate``/``overlap``/``budget``. The effective
    budget is ``budget_tokens`` when given, else :func:`budget_for_attempt`.
    """

    budget = _resolve_budget(budget_tokens, attempt)
    threshold = _finite_float(overlap_threshold, 0.8)
    boost = _finite_float(subtree_weight, 1.5)
    ordered = _coerce_candidates(candidates)
    remaining = list(enumerate(ordered))
    selected: list[MemCreditCandidate] = []
    skipped: list[SkippedCandidate] = []
    seen: set[str] = set()
    tokens_used = 0

    while remaining:
        survivors: list[tuple[int, MemCreditCandidate]] = []
        for index, candidate in remaining:
            if not candidate.content.strip():
                skipped.append(SkippedCandidate(candidate.identifier, "empty"))
                continue
            if candidate.identifier in seen:
                skipped.append(SkippedCandidate(candidate.identifier, "duplicate"))
                continue
            overlaps = any(
                token_jaccard(candidate.content, carried.content) > threshold
                for carried in selected
            )
            if overlaps:
                seen.add(candidate.identifier)
                skipped.append(SkippedCandidate(candidate.identifier, "overlap"))
                continue
            survivors.append((index, candidate))
        remaining = survivors
        if not remaining:
            break

        best_index = remaining[0][0]
        best_candidate = remaining[0][1]
        best_density = _density(best_candidate, selected, boost)
        for index, candidate in remaining[1:]:
            density = _density(candidate, selected, boost)
            if density > best_density:
                best_index, best_candidate, best_density = index, candidate, density

        remaining = [
            (index, candidate)
            for index, candidate in remaining
            if index != best_index
        ]
        if tokens_used + best_candidate.tokens > budget:
            seen.add(best_candidate.identifier)
            skipped.append(SkippedCandidate(best_candidate.identifier, "budget"))
            continue
        selected.append(best_candidate)
        seen.add(best_candidate.identifier)
        tokens_used += best_candidate.tokens

    return MemCreditResult(
        selected=tuple(selected),
        skipped=tuple(skipped),
        tokens_used=tokens_used,
        budget=budget,
    )


class _TrieNode:
    """One normalized path segment (TrieHI-style directory tree node)."""

    __slots__ = ("children", "count")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.count = 0


class DirectoryTrie:
    """Segment trie over indexed paths, used for subtree retrieval affinity.

    Paths are normalized to ``/`` separators; adding the same path twice is
    idempotent. A path is counted both at its own node and at each ancestor
    node, so :meth:`prefix_count` is the number of indexed paths under a
    directory prefix (including an exact match).
    """

    def __init__(self) -> None:
        self._root = _TrieNode()
        self._paths: set[str] = set()

    def __len__(self) -> int:
        return len(self._paths)

    def add(self, path: str) -> None:
        """Index one path (file or trailing-slash directory)."""

        normalised = _normalise_path(path)
        if not normalised or normalised in self._paths:
            return
        self._paths.add(normalised)
        node = self._root
        node.count += 1
        for segment in normalised.split("/"):
            node = node.children.setdefault(segment, _TrieNode())
            node.count += 1

    def prefix_count(self, prefix: str) -> int:
        """How many indexed paths start with ``prefix`` (segment boundary)."""

        normalised = _normalise_path(prefix)
        node = self._root
        segments = normalised.split("/") if normalised else []
        for segment in segments:
            child = node.children.get(segment)
            if child is None:
                return 0
            node = child
        return node.count

    def best_prefix(self, path: str) -> str:
        """Deepest indexed directory prefix of ``path`` (empty when none)."""

        normalised = _normalise_path(path)
        if not normalised:
            return ""
        segments = _parent_segments(path)
        node = self._root
        found: list[str] = []
        for segment in segments:
            child = node.children.get(segment)
            if child is None:
                break
            node = child
            if node.count > 0:
                found.append(segment)
        return "/".join(found)

    def subtree_affinity(self, path: str) -> float:
        """1.5 when another indexed path shares an ancestor directory, else 1.0.

        Root-level paths have no ancestor directory and therefore score 1.0,
        matching :func:`directory_affinity`.
        """

        normalised = _normalise_path(path)
        if not normalised:
            return 1.0
        ancestors = set(_parent_segments(path))
        if not ancestors:
            return 1.0
        for stored in self._paths:
            if stored == normalised:
                continue
            other = set(_parent_segments(stored))
            if ancestors & other:
                return _SUBTREE_AFFINITY
        return 1.0


def memcredit_enabled() -> bool:
    """Whether ``UC_MEMCREDIT`` enables the arbiter (fail closed)."""

    value = os.environ.get(MEMCREDIT_ENABLED_ENV, "")
    return value.strip().lower() in _TRUTHY_VALUES
