# 53 — Shortcut hints stop lying (T53)

**What went wrong:** four places told the user which keys switch views, and every one was
stale — three had drifted independently, one was incomplete:

| Where | Said | Truth |
|---|---|---|
| overview help line | `1–7` | keys are `1`–`9` **and** `0` |
| footer keybar | `1–9` | `0` (receipts, D24) missing |
| `?` help sheet, view row | `1 … 7` | same as above |
| `?` help sheet, letters | O T W S M E I | **A** (approvals) missing |

**Fix:** derive instead of restate.

- `viewKeyHint(keys)` in `dashboard/src/lib/terminalView.ts` turns the actual key list
  into the hint (`0–9`; a gap stays a gap: `1·2·4`). All three numeric hints now call it
  with `VIEWS.map((item) => item.key)`.
- `LETTER_VIEWS` moved from the component into the same lib module, beside
  `TERMINAL_VIEWS`, so the key handler and the help sheet read one table; the help sheet
  renders its letter rows from `Object.entries(LETTER_VIEWS)` with labels from `VIEWS`.

**Tests:** `dashboard/tests/terminalView.test.mjs` pins the hint function (contiguous,
unsorted input, a gap, duplicates, empty) and asserts every shortcut letter points at a
registered view and that letters are unique.

**Status:** implemented (`dashboard/src/lib/terminalView.ts`,
`dashboard/src/components/terminal/TerminalDashboard.tsx`,
`dashboard/tests/terminalView.test.mjs`)

**Boundary:** no shortcut was added or removed — receipts stays on `0` and still has no
letter; the point is that what the UI says now matches what the handler does.
