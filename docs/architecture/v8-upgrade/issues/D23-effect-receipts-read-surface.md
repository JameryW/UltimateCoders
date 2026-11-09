# D23 — Effect receipts have a read surface

Status: decided
Decided: 2026-11-09

## Question

An `UNKNOWN` or expired-lease `RECONCILING` receipt is the one thing an operator must act
on, and today the only way to find them is to open the runtime-state database by hand.
`EffectLedger.unknown()` / `reconciling()` exist, but nothing exposes them. Should the
Dashboard grow a read surface for receipts — and if so, what may it do?

## Decision

**Yes: a read-only, authenticated list endpoint. It never mutates a receipt.**

1. `GET /dashboard/api/effects` returns receipts from the runtime-state store, filtered
   by `status` (default `UNKNOWN` — the actionable queue), with `limit` (1–1000, default
   100) and `after` (effect-id key cursor). Response:
   `{effects, count, total, next_after}`; each row carries `effect_id`, `kind`,
   `status`, `owner`, `lease_until`, `lease_expired`, `updated_at`, `reason`, `request`.
2. **`stale=1`** narrows to receipts whose soft lease has expired — the crashed-owner
   case T37 made takeover-able; asking for it with a status other than `RECONCILING` is
   a 400 (it would mean nothing).
3. **No mutation.** Reconciling stays with `inference.reconcile` (identity, CAS
   `expected_version`, audit) and with the merge arbiter. A dashboard button would
   duplicate an authority that already has an identity model — and the dashboard has no
   operator principal to attribute the action to.
4. **No `request` redaction beyond what is stored.** Receipts already hold only ids,
   branch names, digests and counts; no prompt content or diffs reach this table (D17).

## Why

The receipts are only useful if the queue is visible: T36–T38 made reconciliation
possible and safe, but an operator who cannot see "what is stuck and who holds it" will
never run it. This is the read half of the same decision surface, and it follows the
existing dashboard conventions (auth, bounded window, key cursor).

## Boundary

- No resolve/kill/disarm action; no write of any kind.
- No new storage or index: the endpoint queries the namespace the ledger already writes.
- No cross-host aggregation — it reports the store this process reads.

## Implementation ticket

T43: the route + guards + tests.
