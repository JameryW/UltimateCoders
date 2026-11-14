# 70 — The showcase scrolls, and says what it actually is (T70)

**Symptom.** `https://uc.jameryw.dev/` rendered the full product overview — five sections,
`documentElement.scrollHeight` 3688 against `innerHeight` 900 — and no user input moved it.
Measured over CDP on the deployed bundle: six 400px `mouseWheel` events and a touch drag left
`window.scrollY` at `0`, while `getComputedStyle(document.body).overflowY` reported `hidden`.

**Root cause.** `dashboard/src/index.css` pinned the document for the *operations shell*:

```css
body { margin: 0; overflow: hidden; ... }
```

That rule was inherited by every route. `<html>` is `overflow: visible`, so per the CSS overflow
propagation rules the body's value governs the viewport: the viewport became `hidden` and the
wheel had nothing to scroll. `.home-page`'s own `overflow-y: auto` could not rescue it — its height
is content-driven, so it never overflows itself and the declaration is inert; it also silently
swallowed the intent, which is why the bug read as "the page is short" rather than "the page is
pinned".

A second, quieter defect sat next to it: the overview mounted `useGrpcWeb({ enabled: true })`
unconditionally. On a static host there is no Gateway behind `/dashboard/api/health` and no
WatchTask stream to attach to, so the page spent the whole visit reconnecting, and the runtime card
had no vocabulary for "this host has no live API".

**Fix (scroll).**
- The base rule is now `body { margin: 0; overflow-x: hidden; ... }` with a comment explaining the
  propagation, so the document scrolls and sideways overflow is still clamped.
- The terminal shell keeps its pin explicitly through the rule it already had —
  `body:has(.terminal-app) { overflow: hidden }` — which is the shell's requirement, not the
  document's.
- `.home-page` drops `overflow-y: auto` (a content-sized element cannot be its own scroller) and
  drops `scroll-behavior`, which did nothing on a non-scrolling element; smooth scrolling moved to
  `html:has(.home-page)`. `min-height: 100dvh` replaces the `vh` value so mobile browser chrome
  cannot clip the hero.
- Anchor targets gained `scroll-margin-top: 92px` (72px on phones) so a nav jump does not land
  under the sticky header.

**Fix (the page itself).**
- A sticky top bar (`position: sticky; top: 0`, blurred translucent background) carrying the nav,
  with a 1px `scaleX` scroll-progress line driven by `animation-timeline: scroll(root block)`
  (`@supports`-guarded, so browsers without scroll timelines simply show the resting line).
- A keyboard skip link (`:focus-visible` slides it in) and a focusable `#home-content` target.
- Phone nav: the three `:not(:last-child)` / `:nth-last-child(n)` rules that *hid* links by
  position are gone — they would hide the wrong link the moment one was added. The nav is now a
  horizontally scrollable row with snap points, so all five remain reachable.
- The runtime card is honest about its host: a 2.5s same-origin probe of
  `/dashboard/api/health` decides `reachable` only on `2xx` **and** a JSON content-type (a static
  host answers 200 `text/html` for the SPA fallback, and 502 when the orchestrator is down). Until
  the probe is reachable the page renders `PREVIEW` with "Static preview · no Gateway behind this
  host" and no live refresh; `useGrpcWeb` is gated on `probe === "reachable"`, so nothing retries
  in the background. Polling backs off 30s → 15s·2ⁿ capped at 120s and stops after three failures
  instead of retrying forever.
- Sync age ("just now" / "42s ago" / "3m ago", future clocks clamped), `aria-current` scroll-spy
  over the four nav sections, and a document title the overview owns while mounted.

**Tests** (`dashboard/tests/homeRuntime.test.mjs`, `dashboard/tests/homeSurface.test.mjs` — 18
cases): the pure helpers are pinned by table (probe classification matrix, the mode table, badge and
label coverage, poll-delay monotonicity/cap and the exact stop threshold, sync-age formatting, the
title). The scroll fix is pinned structurally against the stylesheet and the page: the base `body`
rule must not contain `overflow: hidden`/`overflow-y: hidden` and must clamp `overflow-x`; the
shell rule must still contain `overflow: hidden`; `.home-page` must not be a scroller and must not
carry `scroll-behavior`; the sticky bar, progress line, skip link and ≥64px scroll margin must
exist; the fragile `nth-child` nav rules must stay deleted; the page must not match
`useGrpcWeb({ enabled: true`, must gate on the probe, and every nav id must exist as an element id.

**Measured before/after** — same harness (`.scratch/wheel_probe.mjs`, headless Edge over CDP),
same 1440×900 viewport, against two builds served from `dist`:
- unfixed bundle (body pinned again): `scrollHeight` 3677, `wheelScrollY` 0, `touchScrollY` 0;
- fixed bundle: `bodyOverflowY` `auto`, `wheelScrollY` 2400, `touchScrollY` 2777,
  `scrollWidth === clientWidth` (no horizontal drift), top bar `position: sticky` with
  `getBoundingClientRect().top === 0` while scrolled, progress line
  `matrix(0.864242, …)`, `aria-current` on "Execution flow", and a real Tab focuses the skip link
  at `top: 10`;
- 390×844: `scrollHeight` 6586, `touchScrollY` 2807, all five nav links visible, `scrollWidth 381
  === clientWidth 381`;
- operations-shell regression check: injecting `.terminal-app` into the live document flips
  `bodyOverflowY` back to `hidden` and returns `pinnedWheelScrollY` 0, so the shell still pins the
  document it needs.
Four negative controls (back up → mutate → run → restore) all fail the guard and pass again:
body re-pinned, `.home-page` made a scroller again, `enabled: true`, and one `nth-child` nav rule
restored.

**Status:** implemented (`dashboard/src/index.css`, `dashboard/src/pages/HomePage.tsx`,
`dashboard/src/lib/homeRuntime.ts`, `dashboard/tests/homeRuntime.test.mjs`,
`dashboard/tests/homeSurface.test.mjs`)

**Boundary:** this repository does not deploy the dashboard — `docker/Dockerfile.dashboard` builds
`dist` and copies it into nginx, and no workflow publishes it. The fix therefore lands in source and
in CI; `https://uc.jameryw.dev/` keeps serving the old bundle until the operator rebuilds the image
(or re-uploads `dist`), which is the only step that can be seen in a browser on that host.
