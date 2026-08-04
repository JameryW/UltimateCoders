# Remove dead Python rate_limiter.py

## Goal

Delete the orphaned Python `RateLimiter` class (`python/ultimate_coders/agent/rate_limiter.py`). The Rust `LlmRateLimiter` (`crates/uc-engine/src/rate_limiter.rs`, fixed in #541) is the real rate limiter, wired into the gateway. The Python class is never instantiated (no imports, no `__init__.py` export) — same dead-code pattern as the scheduler.py removal (#548).

## What I already know

* `python/ultimate_coders/agent/rate_limiter.py` — `RateLimiterConfig` + `RateLimiter` class. No callers (grep: no `from ... import`, no `__init__` export).
* Rust `LlmRateLimiter` (uc-engine/src/rate_limiter.rs) — the real rate limiter, wired into `LocalEngine` (#541 fixed the RPM-leak bug, #544+ uses it).
* `dashboard/metrics.py` has `rate_limiter_remaining_ratio`/`threshold_pct` fields — but those are metric data fields, NOT the Python `RateLimiter` class. Unrelated.
* No test file (`test_rate_limit*.py` doesn't exist).

## Requirements

* Delete `python/ultimate_coders/agent/rate_limiter.py`.
* No `__init__.py` edit needed (it's not exported there).
* Verify `python3 -c "import ultimate_coders.agent"` succeeds (no stale ref).
* Verify `ruff` clean.
* Verify `pytest tests/python/` passes (no test depends on it).

## Acceptance Criteria

* [ ] `agent/rate_limiter.py` deleted
* [ ] `import ultimate_coders.agent` succeeds
* [ ] `pytest tests/python/` passes
* [ ] ruff clean
* [ ] CI green

## Out of Scope

* Touching the Rust rate limiter (#541 done)
* The metrics.py data fields (unrelated)

## Technical Notes

* `python/ultimate_coders/agent/rate_limiter.py` — the dead file
* `crates/uc-engine/src/rate_limiter.rs` — the real (Rust) rate limiter
* [[python-worker-audit-2026-08-03]] — flagged rate_limiter as dormant
* #548 (scheduler.py removal) — the pattern to mirror
