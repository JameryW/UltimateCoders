# Add search_cache.py test coverage

## Goal

`python/ultimate_coders/agent/search_cache.py` (`WorkerLocalCache`) is wired into `nats_worker.py` (cross-worker search cache invalidation, line 2514) but has NO test file. Add test coverage for the cache's public API.

## What I already know

* `WorkerLocalCache` (search_cache.py:22): `get_search`/`put_search`/`get_repos`/`put_repos`/`invalidate`/`stats`/`search_key`.
* TTL-based (entries expire after `ttl_seconds`).
* Used by `nats_worker.py:2514` (`self._worker._search_cache.invalidate()`).
- `get_default_cache()` singleton accessor.

## Requirements

* New `tests/python/test_search_cache.py` covering: put+get (hit), TTL expiry (miss after TTL), `invalidate` (prefix + all), `stats`, `get_repos`/`put_repos`, `search_key` generation, `get_default_cache` singleton.
* `pytest tests/python/test_search_cache.py` passes.
* `ruff` clean.

## Acceptance Criteria

* [ ] test_search_cache.py created
* [ ] All WorkerLocalCache public methods tested
* [ ] TTL expiry tested (use a short TTL + sleep)
* [ ] pytest + ruff pass
* [ ] CI green

## Out of Scope

* The engine.py search cache (different cache, already has indirect coverage)

## Technical Notes

* `python/ultimate_coders/agent/search_cache.py` — the module under test
* `python/ultimate_coders/nats_worker.py:2514` — the usage site
