"""SearchResult — result types for search operations."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchResultItem:
    """A single item in the search results."""
    repo_id: str
    file_path: str
    start_line: int
    end_line: int
    content_snippet: str
    match_type: str
    score: float
    symbol_name: str | None = None
    symbol_kind: str | None = None
    parent_symbol: str | None = None

    @property
    def location(self) -> str:
        return f"{self.repo_id}:{self.file_path}:{self.start_line}"


@dataclass
class SearchResult:
    """Collection of search results."""
    items: list[SearchResultItem] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    def sorted_by_score(self) -> SearchResult:
        return SearchResult(
            items=sorted(self.items, key=lambda x: x.score, reverse=True)
        )
