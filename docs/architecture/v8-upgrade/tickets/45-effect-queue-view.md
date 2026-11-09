# 45 — Receipt queue view (T45)

**What to build:** the `#/dashboard/effects` view — lib helpers, pure list, fetching
queue with status select + `lease expired` filter + load-more, and the view
registration — per [spec-p14.md](../spec-p14.md).

**Blocked by:** D24

**Status:** implemented (`dashboard/src/lib/effects.ts`,
`dashboard/src/components/panels/EffectQueuePanel.tsx`,
`dashboard/src/lib/terminalView.ts`,
`dashboard/src/components/terminal/TerminalDashboard.tsx`; tests in
`dashboard/tests/effects.test.mjs`)

- [x] `effects` view registered and addressable by hash
- [x] `UNKNOWN` default; `stale` offered only for `RECONCILING`
- [x] Rows: `kind · STATUS · age`, holder (with "lease expired"), scope
- [x] Load-more appends deduped rows; no mutation control
