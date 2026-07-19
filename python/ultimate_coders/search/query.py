"""SearchQuery builder — Pythonic interface for constructing search queries."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ponytail: F66 — bounds for limit(); negative/huge values passed straight
# through before (the engine would reject or over-fetch).
_MIN_RESULTS = 1
_MAX_RESULTS = 1000


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
            # ponytail: F66 — failing silently widened the search to ALL repos
            # (empty repo_ids = unscoped); surface it so a broken repo list is
            # visible instead of looking like a normal wide search.
            logger.warning(
                "list_repos failed; search scope widened to all repositories",
                exc_info=True,
            )
        return self

    def in_workspace(self, engine: object, workspace_id: str) -> SearchQuery:
        """Scope search to all repos in a workspace.

        Looks up all repos belonging to ``workspace_id`` via the engine and
        restricts the search to those repo IDs.

        Args:
            engine: An Engine instance with list_repos(workspace_id=...) support.
            workspace_id: Workspace ID to scope the search to.

        Returns:
            self for chaining.
        """
        try:
            repos = engine.list_repos(workspace_id=workspace_id)
            self._repo_ids = [
                r.repo_id if hasattr(r, "repo_id") else str(r)
                for r in repos
            ]
        except Exception:
            # ponytail: F66 — failing silently widened the search to ALL repos
            # (empty repo_ids = unscoped); surface it so a broken repo list is
            # visible instead of looking like a normal wide search.
            logger.warning(
                "list_repos failed; search scope widened to all repositories",
                exc_info=True,
            )
        return self

    def in_languages(self, languages: list[str]) -> SearchQuery:
        self._languages = languages
        return self

    def with_modes(self, modes: list[str]) -> SearchQuery:
        self._modes = modes
        return self

    def limit(self, max_results: int) -> SearchQuery:
        # ponytail: F66 — clamp; negative/huge values previously passed through.
        self._max_results = max(_MIN_RESULTS, min(_MAX_RESULTS, int(max_results)))
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
