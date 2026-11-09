# Spec P13 — Effect receipts read surface

Blocked by: D23 (decided). Read-only; no new storage, no mutations.

## What to build

1. `GET /dashboard/api/effects` in `python/ultimate_coders/dashboard/app.py`:
   - auth via the existing `_check_auth`;
   - `status` ∈ `PREPARED | IN_FLIGHT | UNKNOWN | RECONCILING | CONFIRMED | FAILED`,
     default `UNKNOWN`;
   - `limit` 1–1000 (default 100); `after` = effect-id key cursor;
   - `stale=1` only with `status=RECONCILING`, filtering rows whose `lease_until` passed;
   - response `{effects, count, total, next_after}`; each row adds a computed
     `lease_expired` boolean.
2. Tests in `tests/python/test_dashboard_effects.py`: auth, default status, invalid
   status/limit/stale-combination → 400, paging by `after`, `stale=1` filtering and
   `lease_expired` computation, and that a `CONFIRMED` receipt is not in the default view.

## Acceptance criteria

- [ ] Unauthenticated call is refused like the other API routes.
- [ ] Default view is the `UNKNOWN` queue; an explicit status selects that state.
- [ ] `after` returns the next window; `next_after` is null on a short page.
- [ ] `stale=1` returns only expired-lease `RECONCILING` rows, with `lease_expired: true`.
- [ ] Bad `status` / out-of-range `limit` / `stale` without `RECONCILING` → 400.
- [ ] Nothing in the route writes to the store.

## Out of scope

- Any mutating action (reconcile stays in the operator CLI)
- Cross-host aggregation
- A dashboard panel (may follow under D19's precedent)
