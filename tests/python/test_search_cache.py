"""Tests for WorkerLocalCache (search_cache.py).

WorkerLocalCache is a TTL + LRU cache for cross-repo search results and
repo listings, wired into nats_worker.py for cross-worker invalidation.
These tests cover the full public API: get_search/put_search, get_repos/
put_repos, invalidate (prefix + all), stats, search_key generation,
TTL expiry, and the get_default_cache singleton.
"""

from __future__ import annotations

import time

from ultimate_coders.agent.search_cache import (
    WorkerLocalCache,
    get_default_cache,
)


def test_put_get_search_hit():
    """put_search then get_search returns the cached value."""
    cache = WorkerLocalCache()
    key = "some-key"
    payload = {"items": [1, 2, 3], "total": 3}
    cache.put_search(key, payload)
    assert cache.get_search(key) is payload


def test_get_search_miss():
    """get_search returns None for a key that was never stored."""
    cache = WorkerLocalCache()
    assert cache.get_search("never-stored") is None


def test_search_ttl_expiry():
    """After ttl_seconds elapses, get_search returns None (expired)."""
    cache = WorkerLocalCache(ttl_seconds=0.1)
    cache.put_search("k", "v")
    assert cache.get_search("k") == "v"
    time.sleep(0.15)
    assert cache.get_search("k") is None
    # Expired entry is also evicted from the internal dict.
    assert "k" not in cache._search


def test_search_lru_eviction():
    """Oldest entry is evicted once max_search_entries is exceeded."""
    cache = WorkerLocalCache(max_search_entries=2)
    cache.put_search("a", 1)
    cache.put_search("b", 2)
    cache.put_search("c", 3)  # evicts "a" (oldest)
    assert cache.get_search("a") is None
    assert cache.get_search("b") == 2
    assert cache.get_search("c") == 3


def test_invalidate_by_prefix():
    """invalidate(prefix) removes only search keys starting with prefix."""
    cache = WorkerLocalCache()
    cache.put_search("repo1:query1", "r1q1")
    cache.put_search("repo1:query2", "r1q2")
    cache.put_search("repo2:query1", "r2q1")
    cache.invalidate("repo1:")
    assert cache.get_search("repo1:query1") is None
    assert cache.get_search("repo1:query2") is None
    assert cache.get_search("repo2:query1") == "r2q1"


def test_invalidate_all():
    """invalidate() with empty prefix clears both search and repos caches."""
    cache = WorkerLocalCache()
    cache.put_search("k1", "v1")
    cache.put_repos(["repo-a"])
    cache.invalidate()
    assert cache.get_search("k1") is None
    assert cache.get_repos() is None
    assert cache.stats == {"search_entries": 0, "repo_entries": 0}


def test_get_put_repos_hit():
    """put_repos then get_repos returns the cached listing."""
    cache = WorkerLocalCache()
    repos = [{"repo_id": "r1"}, {"repo_id": "r2"}]
    cache.put_repos(repos)
    assert cache.get_repos() is repos


def test_get_repos_miss():
    """get_repos returns None when nothing cached."""
    cache = WorkerLocalCache()
    assert cache.get_repos() is None


def test_repos_custom_key():
    """put_repos/get_repos accept a custom key namespace."""
    cache = WorkerLocalCache()
    cache.put_repos(["r1"], key="custom")
    assert cache.get_repos("custom") == ["r1"]
    # default key still empty
    assert cache.get_repos() is None


def test_repos_ttl_expiry():
    """Repo listing also expires after ttl_seconds."""
    cache = WorkerLocalCache(ttl_seconds=0.1)
    cache.put_repos(["r1"])
    assert cache.get_repos() == ["r1"]
    time.sleep(0.15)
    assert cache.get_repos() is None


def test_repos_lru_eviction():
    """Oldest repo entry evicted once max_repo_entries exceeded."""
    cache = WorkerLocalCache(max_repo_entries=1)
    cache.put_repos(["r1"], key="k1")
    cache.put_repos(["r2"], key="k2")  # evicts "k1"
    assert cache.get_repos("k1") is None
    assert cache.get_repos("k2") == ["r2"]


def test_stats_counts_entries():
    """stats reflects the number of search + repo entries currently cached."""
    cache = WorkerLocalCache()
    assert cache.stats == {"search_entries": 0, "repo_entries": 0}
    cache.put_search("a", 1)
    cache.put_search("b", 2)
    cache.put_repos(["r"])
    assert cache.stats == {"search_entries": 2, "repo_entries": 1}


def test_search_key_deterministic():
    """search_key is identical for identical inputs (order-independent for lists)."""
    k1 = WorkerLocalCache.search_key("hello", ["r2", "r1"], ["semantic", "text"], 10)
    k2 = WorkerLocalCache.search_key("hello", ["r1", "r2"], ["text", "semantic"], 10)
    assert k1 == k2


def test_search_key_differs_on_inputs():
    """search_key changes when any input differs."""
    base = WorkerLocalCache.search_key("hello", ["r1"], ["text"], 10)
    assert WorkerLocalCache.search_key("world", ["r1"], ["text"], 10) != base
    assert WorkerLocalCache.search_key("hello", ["r2"], ["text"], 10) != base
    assert WorkerLocalCache.search_key("hello", ["r1"], ["semantic"], 10) != base
    assert WorkerLocalCache.search_key("hello", ["r1"], ["text"], 20) != base


def test_search_key_is_hex_string():
    """search_key returns a hex string (blake2b digest_size=16 → 32 hex chars)."""
    key = WorkerLocalCache.search_key("q", ["r"], ["text"], 5)
    assert isinstance(key, str)
    assert len(key) == 32
    int(key, 16)  # parses as hex


def test_get_default_cache_singleton():
    """get_default_cache returns the same instance across calls."""
    a = get_default_cache()
    b = get_default_cache()
    assert a is b
    assert isinstance(a, WorkerLocalCache)
