"""CodeGrep RL-trained parallel retrieval agent (#75).

The retriever executes a bounded sequence of search actions — ``grep``,
``glob`` and ``read`` — against a caller-supplied in-memory code index.  It
measures retrieval precision and falls back to an 8K-token RepoMap view when
precision falls below the published threshold.  No live repository is read and
no model is invoked; the RL policy is represented by the deterministic action
schedule and precision gate.

Flag: ``UC_CODEGREP_RETRIEVER`` (default off).
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CODEGREP_RETRIEVER_ENV",
    "CodeGrepRetriever",
    "CodeIndex",
    "RetrievalAction",
    "RetrievalHit",
    "RetrievalReport",
    "RetrievalRound",
    "codegrep_retriever_enabled",
]

CODEGREP_RETRIEVER_ENV = "UC_CODEGREP_RETRIEVER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def codegrep_retriever_enabled() -> bool:
    """Return whether the CodeGrep retriever is enabled."""

    return os.environ.get(CODEGREP_RETRIEVER_ENV, "").strip().lower() in _TRUTHY


class RetrievalAction(str, Enum):
    """One retrieval action."""

    GREP = "grep"
    GLOB = "glob"
    READ = "read"


@dataclass(frozen=True)
class CodeSymbol:
    """One indexed code symbol."""

    path: str
    symbol: str
    line: int
    content: str

    def __post_init__(self) -> None:
        if not self.path.strip() or not self.symbol.strip():
            raise ValueError("path and symbol must not be empty")
        if self.line < 1:
            raise ValueError("line must be positive")


@dataclass(frozen=True)
class RetrievalHit:
    """A hit returned by one retrieval action."""

    path: str
    symbol: str
    line: int
    content: str
    score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "line": self.line,
            "content": self.content,
            "score": self.score,
        }


@dataclass(frozen=True)
class RetrievalRound:
    """One action in the retrieval trace."""

    action: RetrievalAction
    query: str
    hits: tuple[RetrievalHit, ...]


@dataclass(frozen=True)
class RetrievalReport:
    """Outcome of one retrieval run."""

    precision: float
    threshold: float
    fallback: bool
    hits: tuple[RetrievalHit, ...]
    rounds: tuple[RetrievalRound, ...]
    repo_map: str

    def to_dict(self) -> dict[str, object]:
        return {
            "precision": self.precision,
            "threshold": self.threshold,
            "fallback": self.fallback,
            "hits": [hit.to_dict() for hit in self.hits],
            "rounds": [
                {
                    "action": round_.action.value,
                    "query": round_.query,
                    "hits": len(round_.hits),
                }
                for round_ in self.rounds
            ],
            "repo_map_chars": len(self.repo_map),
        }


class CodeIndex:
    """A small deterministic symbol index used by the retriever."""

    def __init__(self) -> None:
        self._files: dict[str, dict[str, CodeSymbol]] = {}

    def add_file(self, path: str, symbols: dict[str, str]) -> None:
        path = path.strip()
        if not path:
            raise ValueError("path must not be empty")
        if not symbols:
            raise ValueError("symbols must not be empty")
        target = self._files.setdefault(path, {})
        for symbol, content in symbols.items():
            name = symbol.strip()
            if not name:
                raise ValueError("symbol must not be empty")
            target[name] = CodeSymbol(path, name, 1, content)

    def files(self) -> tuple[str, ...]:
        return tuple(sorted(self._files))

    def symbols(self) -> tuple[CodeSymbol, ...]:
        return tuple(
            symbol
            for path in sorted(self._files)
            for symbol in self._files[path].values()
        )

    def grep(self, term: str) -> tuple[RetrievalHit, ...]:
        needle = term.strip().lower()
        if not needle:
            return ()
        hits: list[RetrievalHit] = []
        for symbol in self.symbols():
            haystack = f"{symbol.symbol} {symbol.content}".lower()
            if needle in haystack:
                score = 1.0 if symbol.symbol.lower() == needle else 0.75
                hits.append(
                    RetrievalHit(
                        symbol.path,
                        symbol.symbol,
                        symbol.line,
                        symbol.content,
                        score,
                    )
                )
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.path, hit.symbol)))

    def glob(self, pattern: str) -> tuple[RetrievalHit, ...]:
        needle = pattern.strip().lower()
        hits: list[RetrievalHit] = []
        for path in self.files():
            if fnmatch.fnmatch(path.lower(), needle) or needle in path.lower():
                for symbol in self._files[path].values():
                    hits.append(
                        RetrievalHit(
                            path,
                            symbol.symbol,
                            symbol.line,
                            symbol.content,
                            0.5,
                        )
                    )
        return tuple(sorted(hits, key=lambda hit: (hit.path, hit.symbol)))

    def read(self, path: str) -> tuple[RetrievalHit, ...]:
        return tuple(
            RetrievalHit(
                symbol.path,
                symbol.symbol,
                symbol.line,
                symbol.content,
                0.4,
            )
            for symbol in self._files.get(path, {}).values()
        )


class CodeGrepRetriever:
    """Run a bounded grep/glob/read policy and gate on retrieval precision."""

    def __init__(
        self,
        precision_threshold: float = 0.6,
        repo_map_budget_tokens: int = 8000,
    ) -> None:
        if not 0.0 <= precision_threshold <= 1.0:
            raise ValueError("precision_threshold must be between 0 and 1")
        if repo_map_budget_tokens < 1:
            raise ValueError("repo_map_budget_tokens must be positive")
        self.precision_threshold = precision_threshold
        self.repo_map_budget_tokens = repo_map_budget_tokens

    def retrieve(
        self,
        query: str,
        index: CodeIndex,
        *,
        relevant_paths: tuple[str, ...] = (),
    ) -> RetrievalReport:
        if not query.strip():
            raise ValueError("query must not be empty")

        rounds: list[RetrievalRound] = []
        grep_hits = index.grep(query)
        rounds.append(RetrievalRound(RetrievalAction.GREP, query, grep_hits))

        if not grep_hits:
            glob_hits = index.glob(f"*{query}*")
            rounds.append(RetrievalRound(RetrievalAction.GLOB, f"*{query}*", glob_hits))
            paths = sorted({hit.path for hit in glob_hits})[:2]
        else:
            paths = sorted({hit.path for hit in grep_hits})[:2]

        read_hits: list[RetrievalHit] = []
        for path in paths:
            hits = index.read(path)
            read_hits.extend(hits)
            rounds.append(RetrievalRound(RetrievalAction.READ, path, hits))

        combined: dict[tuple[str, str], RetrievalHit] = {}
        for hit in (*grep_hits, *read_hits):
            key = (hit.path, hit.symbol)
            previous = combined.get(key)
            if previous is None or hit.score > previous.score:
                combined[key] = hit
        ordered = tuple(
            sorted(combined.values(), key=lambda hit: (-hit.score, hit.path, hit.symbol))
        )

        if relevant_paths:
            relevant = {path.lower() for path in relevant_paths}
            correct = sum(1 for hit in ordered if hit.path.lower() in relevant)
            precision = correct / len(ordered) if ordered else 0.0
        else:
            precision = 1.0 if ordered else 0.0

        fallback = precision < self.precision_threshold
        repo_map = self._repo_map(index) if fallback else ""
        return RetrievalReport(
            precision,
            self.precision_threshold,
            fallback,
            ordered,
            tuple(rounds),
            repo_map,
        )

    def _repo_map(self, index: CodeIndex) -> str:
        limit = self.repo_map_budget_tokens * 4
        lines: list[str] = []
        for path in index.files():
            symbols = ", ".join(symbol.symbol for symbol in index._files[path].values())
            lines.append(f"{path}: {symbols}")
        text = "\n".join(lines)
        return text[:limit]
