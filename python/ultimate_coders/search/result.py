"""SearchResult — result types for search operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


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
    symbol_name: Optional[str] = None
    symbol_kind: Optional[str] = None
    parent_symbol: Optional[str] = None

    @property
    def location(self) -> str:
        return f"{self.repo_id}:{self.file_path}:{self.start_line}"


@dataclass
class SearchResult:
    """Collection of search results."""
    items: List[SearchResultItem] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    def sorted_by_score(self) -> SearchResult:
        return SearchResult(
            items=sorted(self.items, key=lambda x: x.score, reverse=True)
        )
