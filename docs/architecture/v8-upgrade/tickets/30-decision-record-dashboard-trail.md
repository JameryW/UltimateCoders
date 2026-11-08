# 30 — Decision Record dashboard trail (T30)

**What to build:** `dashboard/src/lib/decisions.ts` (types + pure formatters),
`dashboard/src/components/panels/DecisionRecordPanel.tsx` (pure list + bounded trail),
the `TaskDetail.tsx` wiring, and `dashboard/tests/decisions.test.mjs` — per
[spec-p9.md](../spec-p9.md). Pure read surface: no endpoint, no query parameter, no
cursor.

**Blocked by:** D19

**Status:** implemented (`dashboard/src/lib/decisions.ts`,
`dashboard/src/components/panels/DecisionRecordPanel.tsx`,
`dashboard/src/components/panels/TaskDetail.tsx`,
`dashboard/tests/decisions.test.mjs`)

- [x] Verdict / route / `ctx <12 hex>` / local-effects line per attempt
- [x] `+N` path cap and `truncated` marker; `N of M attempts` count line
- [x] Empty and unavailable states say so instead of pretending
- [x] `tsc` (app + node) and `vite build` pass; node tests pass
