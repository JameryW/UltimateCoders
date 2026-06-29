"""WorkerLocalCache — LRU + TTL cache for search results and repo listings.

Reduces gRPC round-trips when Worker repeatedly searches the same queries
or lists indexed repos. Thread-safe via dict operations in single-threaded
asyncio context.

Usage:
    cache = WorkerLocalCache()
    results = cache.get_search(query_hash)
    if results is None:
        results = engine.search(sq)
        cache.put_search(query_hash, results)
"""

from __future__ import annotations

import hashlib
import time
from typing import Any


class WorkerLocalCache:
    """LRU + TTL cache for cross-repo search results and repo listings.

    ponytail: simple dict-based LRU — OrderedDict would be cleaner but
    dict ordering is guaranteed in Python 3.7+. Upgrade path: cachetools.
    """

    def __init__(
        self,
        max_search_entries: int = 100,
        max_repo_entries: int = 10,
        ttl_seconds: float = 300.0,  # 5 minutes
    ) -> None:
        self._max_search = max_search_entries
        self._max_repos = max_repo_entries
        self._ttl = ttl_seconds
        # {key: (value, timestamp)}
        self._search: dict[str, tuple[Any, float]] = {}
        self._repos: dict[str, tuple[list[Any], float]] = {}

    @staticmethod
    def search_key(
        query: str,
        repo_ids: list[str],
        modes: list[str],
        max_results: int,
    ) -> str:
        """Compute cache key for a search query."""
        raw = f"{query}|{','.join(sorted(repo_ids))}|{','.join(sorted(modes))}|{max_results}"
        return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()

    def get_search(self, key: str) -> Any | None:
        """Get cached search result, or None if missing/expired."""
        entry = self._search.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._search[key]
            return None
        return value

    def put_search(self, key: str, value: Any) -> None:
        """Cache a search result."""
        self._search[key] = (value, time.monotonic())
        # Evict oldest if over capacity
        if len(self._search) > self._max_search:
            oldest = next(iter(self._search))
            del self._search[oldest]

    def get_repos(self, key: str = "list_repos") -> list[Any] | None:
        """Get cached repo listing, or None if missing/expired."""
        entry = self._repos.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._repos[key]
            return None
        return value

    def put_repos(self, repos: list[Any], key: str = "list_repos") -> None:
        """Cache a repo listing."""
        self._repos[key] = (repos, time.monotonic())
        if len(self._repos) > self._max_repos:
            oldest = next(iter(self._repos))
            del self._repos[oldest]

    def invalidate(self, prefix: str = "") -> None:
        """Invalidate cache entries. If prefix given, only matching keys."""
        if not prefix:
            self._search.clear()
            self._repos.clear()
        else:
            keys_to_del = [k for k in self._search if k.startswith(prefix)]
            for k in keys_to_del:
                del self._search[k]

    @property
    def stats(self) -> dict[str, int]:
        """Cache statistics."""
        return {
            "search_entries": len(self._search),
            "repo_entries": len(self._repos),
        }


# Singleton — shared across Worker instances in same process
_default_cache: WorkerLocalCache | None = None


def get_default_cache() -> WorkerLocalCache:
    """Get or create the default singleton cache."""
    global _default_cache
    if _default_cache is None:
        _default_cache = WorkerLocalCache()
    return _default_cache
