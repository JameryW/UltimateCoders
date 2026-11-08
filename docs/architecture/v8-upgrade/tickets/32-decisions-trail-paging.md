# 32 — Decision trail paging (T32)

**What to build:** `order=recent` + `before` + `next_cursor` on
`GET /dashboard/api/decisions` (mirroring the context-manifest route), dashboard
`decisionTrailQuery` / `appendDecisionPage` helpers, and panel paging — per
[spec-p10.md](../spec-p10.md). No new outbox method, no cursor for key order.

**Blocked by:** D20

**Status:** implemented (`python/ultimate_coders/dashboard/app.py`,
`dashboard/src/lib/decisions.ts`,
`dashboard/src/components/panels/DecisionRecordPanel.tsx`; tests in
`tests/python/test_dashboard_decisions.py` and `dashboard/tests/decisions.test.mjs`)

- [x] `order=recent` newest-first; `next_cursor` only on a full page
- [x] `before` resumes without duplicates; unknown cursor is 400
- [x] `before` with `order=key` is 400; invalid `order` is 400
- [x] Panel appends pages deduped and keeps rows on a failed page
