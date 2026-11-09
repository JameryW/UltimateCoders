# 43 — Receipts read surface (T43)

**What to build:** `GET /dashboard/api/effects` — the read-only receipt queue — per
[spec-p13.md](../spec-p13.md).

**Blocked by:** D23

**Status:** implemented (`python/ultimate_coders/dashboard/app.py`; tests in
`tests/python/test_dashboard_effects.py`)

- [x] Auth via the shared check; `status` default `UNKNOWN`; `limit` 1–1000
- [x] `after` pages by effect id; `next_after` only on a full page
- [x] `stale=1` restricted to `RECONCILING`, listing only expired leases
- [x] Rows carry `owner`, `lease_until`, `lease_expired`, `reason`, `request`
- [x] No write path in the route
