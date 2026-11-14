# 71 — The showcase stops advertising a dashboard it cannot serve (T71)

**Symptom.** After T70 the overview knew which host it was on: on `https://uc.jameryw.dev/` the probe
returns `absent`, the runtime card says `PREVIEW`, and the footnote says "Static preview · no Gateway
behind this host". The page *around* that card still promised the opposite. Measured over CDP on the
deployed bundle (`.scratch/cta_probe.mjs`):

| element | text | href |
|---|---|---|
| hero primary | `Open dashboard →` | `#/dashboard` |
| final CTA | `Open operations dashboard →` | `#/dashboard` |
| final CTA note | `real Gateway connection · live task events` | — |
| scenarios inline | `Open live dashboard ↗` | `#/dashboard` |
| runtime foot | `auto refresh 30s` | — |

Every one of those lands on the operations shell, which on a static host can only render `OFFLINE`
with a `gRPC · connecting` badge. So the page's strongest claims were exactly the two things the host
cannot do, and they were false the moment the card above admitted the probe was absent.

**Root cause.** The runtime verdict introduced in T70 was only consumed *inside* the runtime card.
The CTAs were literal JSX: `<a href="#/dashboard">Open operations dashboard`, and the closing claim
was a literal string. Nothing connected the probe result to what the page promises, so a new verdict
could not change the copy — the same drift class as T69's push statuses, one layer up in the UI.

**Fix.** One pure derivation owns the promise (`dashboard/src/lib/homeRuntime.ts`):

```ts
export const DASHBOARD_HREF = "#/dashboard";
export const WORKFLOW_HREF = "#home-workflow";

export function showcasePrimaryCta(mode: ShowcaseRuntimeMode): ShowcaseCta
```

- `live` / `connecting` — a Gateway answered the probe — return
  `{ label: "Open operations dashboard", href: DASHBOARD_HREF, note: … }`; the note is
  "Connected Gateway · live task events" only when the transport is actually connected, and
  "Gateway reachable · connecting the event stream" while it is still attaching.
- `checking` / `preview` return `{ label: "Explore the execution loop", href: WORKFLOW_HREF, note:
  "No Gateway on this host: the operations dashboard runs from a local or cluster deployment." }`.
  A visitor on the static showcase is sent to the part of the page that is true there, and told where
  the dashboard does run.

`HomePage` now derives all four surfaces from that one object: the hero primary button, the hero
secondary (the dashboard link stays available, just no longer advertised as live), the closing CTA
with its note, and the scenarios inline link (`Open live dashboard ↗` only when
`primaryIsDashboard`). The route itself is unchanged and still reachable from the nav.

**Tests** (`dashboard/tests/homeRuntime.test.mjs`, `dashboard/tests/homeSurface.test.mjs` — 20 cases,
2 new):
- the truth table over all four modes: a mode that has a Gateway must point at `DASHBOARD_HREF` and
  must not claim "No Gateway"; a mode without one must point at `WORKFLOW_HREF` and must explain
  `/Gateway/` and `/deployment/i`; `preview` may never carry the dashboard label;
- the page must not contain `real Gateway connection`, must use `href={primaryCta.href}` and
  `{primaryCta.note}`, must keep the inline link conditional, and the string `"Open live dashboard"`
  may appear exactly once — inside that conditional.

**Measured before/after** — same harness, headless Edge over CDP, 1440×900:
- deployed bundle (old code): `mode` `PREVIEW`, hero primary `Open dashboard →` → `#/dashboard`,
  final note `real Gateway connection · live task events`, foot `auto refresh 30s`;
- fixed bundle, static host (`vite preview`, no orchestrator): `mode` `PREVIEW`, title
  `UltimateCoders — distributed AI coding`, hero primary `Explore the execution loop ↓` →
  `#home-workflow`, final note the deployment explanation, inline `See the execution loop ↓`, foot
  `no live API on this host`;
- fixed bundle, host with a JSON `/dashboard/api/health` (`.scratch/mock_host.mjs`): `mode` `SYNCING`,
  hero primary `Open operations dashboard →` → `#/dashboard`, final note `Gateway reachable ·
  connecting the event stream`, inline `Open live dashboard ↗` — so the branch flips in a real
  browser, not only in the unit table.

Negative controls (`.scratch/negative_controls_home.py`, back up → mutate → run → restore) now cover
six mutations, all caught and all passing again after restore: body re-pinned, `.home-page` a
scroller again, `enabled: true`, a `nth-child` nav-hide rule restored, every CTA hardcoded to
`#/dashboard`, and the false `real Gateway connection` note restored.

**Status:** implemented (`dashboard/src/lib/homeRuntime.ts`, `dashboard/src/pages/HomePage.tsx`,
`dashboard/tests/homeRuntime.test.mjs`, `dashboard/tests/homeSurface.test.mjs`)

**Boundary:** like T70, this repository does not deploy the dashboard — `docker/Dockerfile.dashboard`
builds `dist` into nginx and no workflow publishes it. The change is visible on
`https://uc.jameryw.dev/` only after the operator rebuilds the image (or re-uploads `dist`); the
deployed CSS filename changes from `assets/index-DxPEmDHa.css` to the new hash, which makes the
redeploy checkable in one request.

**Still deferred (same drift class, not fixed here):** the count claims on the same page —
`home-metrics` hardcodes `01 / 05 / ∞ / 24/7`, the capabilities heading says "Five layers." while
`systemLayers` is an array of 5, and the capability cards carry hand-written ordinals `01`–`06`.
Those should be derived from the arrays so inserting a card cannot leave the copy lying.
