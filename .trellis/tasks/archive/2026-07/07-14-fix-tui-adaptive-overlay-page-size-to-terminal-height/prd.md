# fix(tui): adaptive overlay page size to terminal height

## Problem
Both UC overlays hardcode `maxVisible = 20` items per page:
- `subtask-tree-overlay.ts:49` — `private maxVisible = 20;`
- `task-list-overlay.ts:61` — `private maxVisible = 20;`

On a short terminal (rows < ~24, common in split panes / tmux), the overlay
renders 3 chrome lines (header + hint + blank) + up to 20 item rows + a footer
pagination line = ~24 lines, exceeding the viewport. The compositor clips the
bottom — the user loses the pagination footer and the last few items, and
PgDn's `maxVisible` step is larger than what actually fits, so scrolling
overshoots.

## Root cause
`maxVisible` is a fixed class field, never recomputed from terminal height.
Both overlay components already hold a typed `TUI` reference (`private tui:
TUI`) whose `.terminal.rows` gives the live viewport height — it's just not
read.

## Scope (MVP)
- Replace the fixed `maxVisible = 20` field with a getter that derives the
  page size from the terminal: `maxVisible = clamp(rows - CHROME, 1, 20)`
  where CHROME accounts for the overlay's fixed header/hint/blank lines (~4
  to also reserve the footer). Cap at 20 so a tall terminal doesn't dump 50
  rows into a paginated overlay.
- Apply to BOTH overlays AND the task-list detail-scroll mode (which reuses
  `maxVisible`).
- Guard: if `tui.terminal` is undefined (defensive — tests pass a mock tui
  without terminal), fall back to the current 20. This keeps the existing
  selfchecks (which pass `undefined` as tui or a mock) green without each
  needing a real terminal mock.
- Update both selfchecks: add one assertion that a small terminal height
  (e.g. mock tui with `terminal.rows = 10`) yields a page size < 20 (overlay
  shows fewer rows). Keep all existing render-line-count / retry-wiring
  assertions unchanged.

## Out of scope
- Widget height (progress widget first arg is `ctx.ui`, not `TUI` — no
  `.terminal` access; capped at 10 by host `MAX_WIDGET_LINES` anyway).
- Horizontal width-adaptive truncation (already handled per-line via `width`).
- Subtask-tree depth/indent reflow.
