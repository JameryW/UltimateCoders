# D20 — The decisions trail pages like the context-manifest trail

Status: decided
Decided: 2026-11-08

## Question

P9 gave the Decision Record a dashboard read surface, but that surface can only ever
show the first `limit` attempts: the decisions endpoint takes `limit` and nothing
else, and its key order is one bounded window. For a long task the rest of the trail
is unreachable — the panel can only say "N of M attempts" and stop. Should the
endpoint grow paging, and if so, which contract?

## Decision

**Give `/dashboard/api/decisions` the same `order=recent` + `before` cursor contract
the context-manifest route already has, and let the panel page with it. The key order
stays a bounded window with no cursor.**

1. **`order=recent`** answers a real time window (newest first by `recorded_at`) —
   `DecisionRecordOutbox.recent` already exists and already scans the whole prefix
   before truncating, so this is a route change, not new machinery.
2. **`before`** is the previous page's `next_cursor` (that page's last row key); the
   response resumes strictly after it in the same total order, and an unknown cursor
   in scope is a 400 (`CursorNotFoundError`), never a silent restart.
3. **`next_cursor`** is non-null only when the page was full; a short page means the
   end. It is `null` for `order=key`, because key order is a single oldest-key window
   (a LIMIT) and a cursor there would be a different mechanism pretending to be the
   same one — asking for it is a 400, exactly like the manifest route.
4. **`count` / `total` keep their meaning**: the window size and the scan count in
   scope. A truncated window still says so.
5. **The panel pages instead of truncating**: "load more" fetches the page after the
   cursor and appends it deduped by key, keeping the rows it already has if a page
   fails.

## Why

The audit trail is only complete if the whole trail is reachable. The outbox and its
cursor already exist; the route is the only thing missing, and the manifest route next
to it is the proven shape to mirror. Two adjacent audit trails answering the same
question in two different contracts is the kind of drift this repository keeps paying
for.

## Boundary

- No new outbox method, no new namespace, no cursor for key order.
- No mutation surface: the route stays read-only and auth-gated like its neighbour.
- The panel does not re-sort: order belongs to the endpoint.

## Implementation ticket

T32: route `order`/`before`/`next_cursor` + endpoint tests; panel paging + helper
tests.
