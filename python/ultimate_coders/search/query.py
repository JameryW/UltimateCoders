"""SearchQuery builder — Pythonic interface for constructing search queries."""

from __future__ import annotations


class SearchQuery:
    """Builder for search queries.

    Usage:
        query = (SearchQuery("authentication logic")
                 .in_repos(["my-backend", "my-frontend"])
                 .in_languages(["python", "rust"])
                 .with_modes(["semantic", "ast"])
                 .limit(20))
    """

    def __init__(self, query: str):
        self._query = query
        self._modes: list[str] = ["hybrid"]
        self._repo_ids: list[str] = []
        self._languages: list[str] = []
        self._path_patterns: list[str] = []
        self._max_results: int = 10

    def in_repos(self, repo_ids: list[str]) -> SearchQuery:
        self._repo_ids = repo_ids
        return self

    def in_all_repos(self, engine: object) -> SearchQuery:
        """Set repo scope to all indexed repos discovered via engine.list_repos().

        Args:
            engine: An Engine instance with list_repos() support.

        Returns:
            self for chaining.
        """
        try:
            repos = engine.list_repos()
            self._repo_ids = [
                r.repo_id if hasattr(r, "repo_id") else str(r)
                for r in repos
            ]
        except Exception:
            # ponytail: if list_repos fails, leave repo_ids empty (searches all)
            pass
        return self

    def in_languages(self, languages: list[str]) -> SearchQuery:
        self._languages = languages
        return self

    def with_modes(self, modes: list[str]) -> SearchQuery:
        self._modes = modes
        return self

    def limit(self, max_results: int) -> SearchQuery:
        self._max_results = max_results
        return self

    def to_dict(self) -> dict:
        return {
            "query": self._query,
            "modes": self._modes,
            "repo_ids": self._repo_ids,
            "languages": self._languages,
            "path_patterns": self._path_patterns,
            "max_results": self._max_results,
        }
