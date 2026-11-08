# Spec P10 — Decision trail paging (`order=recent` + `before` cursor)

Blocked by: D20 (decided). Mirrors the context-manifest route; no new outbox method.

## Goal

Make a long task's Decision Record trail fully reachable: the decisions endpoint
gains the `order=recent` time window and a `before` cursor, and the dashboard panel
pages with it instead of stopping at the first window.

## What to build

1. **Endpoint** (`GET /dashboard/api/decisions`):
   - `order=key` (default) — unchanged bounded key window, `next_cursor: null`;
   - `order=recent` — newest-first by `recorded_at` via `DecisionRecordOutbox.recent`;
   - `before=<key>` — resume strictly after that anchor; unknown cursor in scope is 400;
   - `before` without `order=recent` is 400; invalid `order` is 400;
   - `next_cursor` is the page's last key only when the page was full.
2. **Dashboard lib**: `decisionTrailQuery(taskId, {limit, order, before})` (encoded),
   `appendDecisionPage(trail, payload)` (dedupe by key, keep the cursor),
   `decisionTrail` initial state.
3. **Panel**: asks for `order=recent`, keeps the loaded rows on a failed page, offers
   "load more" while a cursor exists, and states the count line unchanged.
4. **Tests**: endpoint (recent order, paging resumes without duplicates, short page has
   no cursor, key order rejects `before`, unknown cursor is 400) and dashboard
   (query/merge helpers + SSR unchanged).

## Acceptance criteria

- [ ] `order=recent` returns newest-first and a full page reports `next_cursor`.
- [ ] Paging with `before` continues the same order without duplicates.
- [ ] A short page reports `next_cursor: null`.
- [ ] `before` with `order=key` and an unknown cursor are both 400.
- [ ] `count` / `total` keep their existing meaning; auth unchanged.
- [ ] The panel appends pages deduped and keeps rows when a page fails.

## Out of scope

- New outbox methods or namespaces
- A cursor for key order
- Any mutation surface
