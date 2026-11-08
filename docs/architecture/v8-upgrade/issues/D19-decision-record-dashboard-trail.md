# D19 — Decision Records get a dashboard read surface

Status: decided
Decided: 2026-11-08

## Question

The attempt-scoped Decision Record has existed since P0 and now carries the attempt's
local effects (P7), but nothing reads it except the raw
`GET /dashboard/api/decisions` endpoint — an API with no consumer. Should the evidence
stay write-only, or get a read surface, and what may that surface show?

## Decision

**Add a task-scoped Decision Record trail to the dashboard's task detail view. It
renders only fields the record already stores, and it never invents an order or a
total.**

1. **Placement.** A `DecisionRecordTrail` panel next to the existing context-compile
   trail in `TaskDetail.tsx` — the same task-detail surface an operator already uses.
2. **What it shows per attempt:** the terminal verdict (`succeeded` /
   `failed (<error_class>)`), the route the attempt took (stage · risk · model), a
   short context pointer (`ctx <12 hex>`), and the P7 local-effects line
   (`N files (paths, +K) · tools: … · truncated`).
3. **What it never shows:** prompt content, diffs, tool output, or the context
   summary. The record cannot carry them, and the panel reads known fields only.
4. **Ordering and counts belong to the endpoint.** The decisions route returns its key
   window as-is and has no cursor; the panel asks for one bounded window (`limit=100`)
   and says `N of M attempts` when `total` is larger — it never claims the window is
   the whole trail, and it does not re-sort.
5. **No API change.** No new endpoint, no new query parameter, no cursor.

## Why

The evidence chain is only worth what an auditor can actually read. The API has been
write-only since P0; P7 made the records worth reading. The panel is a pure renderer
over the existing response, so it adds no backend surface and no second source of
truth.

## Boundary

- Not a governance approval surface; it never mutates anything.
- No cursor paging (the route has none) — the count line states the truncation instead.
  **Superseded by [D20](D20-decisions-trail-paging.md):** the route now pages with
  `order=recent` + `before`, and the panel loads older attempts.
- No subtask-scope selector in this slice (the endpoint supports it; adding a control
  without a cursor would just narrow a bounded window).

## Implementation ticket

T30: `dashboard/src/lib/decisions.ts` formatters + `DecisionRecordPanel.tsx` +
`dashboard/tests/decisions.test.mjs` (helpers + SSR render) + the `TaskDetail` wiring.
