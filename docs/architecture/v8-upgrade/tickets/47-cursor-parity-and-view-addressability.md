# 47 — Cursor parity and view addressability (T47)

**What to build:** Two loose ends from P13/P14, both about not lying to the operator:

1. `GET /dashboard/api/effects?after=…` accepted any string and answered an empty page —
   a typo looked like "the queue ended". It now 400s (`Unknown cursor in this scope`),
   the rule the decisions route already applies (D20), so the contract does not depend on
   which read surface you are on.
2. The new `effects` view was only covered by `tsc`: nothing proved D24's "addressable by
   hash". `terminalView.test.mjs` now asserts `#/dashboard/effects` both ways, that every
   registered view round-trips through `hashForView`, and that `effects` is registered.

**Blocked by:** D23 (cursor rule), D24 (view)

**Status:** implemented (`python/ultimate_coders/dashboard/app.py`,
`dashboard/tests/terminalView.test.mjs`; tests in `tests/python/test_dashboard_effects.py`)

- [x] Unknown `after` → 400 with the same message family as decisions
- [x] A valid cursor still pages (existing test)
- [x] `effects` view round-trips and is in `TERMINAL_VIEWS`
