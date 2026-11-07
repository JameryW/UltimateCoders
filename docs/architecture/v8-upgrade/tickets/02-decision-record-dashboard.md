# 02 — Decision Record dashboard read API

Status: implemented
Blocked by: 01

## What to build

Expose Decision Records on the Dashboard API, mirroring the auth and pagination guards of `/dashboard/api/context/manifests` (P0: key-order history + `total` is sufficient; `order=recent` optional if the outbox already supports it).

## Acceptance criteria

- `GET /dashboard/api/decisions?task_id=…` requires auth; 400 without `task_id`
- Optional `subtask_id` scopes to one subtask
- Response: `{ "decisions": [...], "count": N, "total": M }`
- Unavailable state → same unavailable helper pattern as context manifests
- Tests in `tests/python/test_dashboard_decisions.py`
