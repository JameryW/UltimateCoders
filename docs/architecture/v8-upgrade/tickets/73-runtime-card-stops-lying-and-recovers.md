# 73 — The runtime card stops saying SYNCING after it stopped trying (and can recover)

**Symptom.** On a deployment that *does* have a Gateway, the overview polls
`EngineService/Health` every 30 s. When the Gateway starts failing, the card goes
`SYNCING` / "Connecting to Gateway transport" and backs off 15 s → 30 s → 60 s.
At the third consecutive failure `shouldStopPolling` returns true, the tick chain
returns, and **no request is ever sent again** — yet the card stays on
`SYNCING` / "Connecting to Gateway transport" for as long as the tab lives. The
badge claims work that is not happening.

A second defect sat behind it: the poll effect was keyed on `[probe, refreshRuntime]`.
Probing a Gateway that answered once then died leaves `probe === "reachable"`
unchanged, so a manual **refresh** that succeeded — the only way back — reset the
failure counters and updated the card, but the loop never restarted. Auto-refresh
stayed dead until a page reload.

**Root cause.** Two independent sources of truth for one question ("is the runtime
live?"): the loop decided internally at the failure threshold, and the card was
derived only from `connected` + `consecutiveFailures`, whose only stop-aware state
was missing. So the *stopped* condition had no representation in the UI, and the
loop's lifecycle was keyed on the probe — a value that cannot change when a
previously reachable Gateway goes away.

**Fix.** `dashboard/src/lib/homeRuntime.ts`:

- `ShowcaseRuntimeMode` gains `"stalled"` and `ShowcaseRuntimeBadge` gains `"OFFLINE"`;
  `showcaseRuntimeMode` returns `"stalled"` once `shouldStopPolling(consecutiveFailures)`
  is true (for both connected and disconnected transports), before the live/connecting
  branches.
- `runtimeStatusLabel("stalled")` = `Gateway stopped answering` (it must not contain
  "connecting" — that is the sentence that lied).
- `pollingActive(mode)` — the loop's only gate: `live` and `connecting` poll, everything
  else does not.
- `showcasePrimaryCta("stalled")` keeps the dashboard destination (the probe did find a
  Gateway here) but with the note "Gateway stopped answering on this host: the dashboard
  reports OFFLINE until it recovers." — it may not borrow the connected claim.

`dashboard/src/pages/HomePage.tsx` keys the poll effect on the mode
(`[mode, refreshRuntime]`), so reaching `stalled` cancels the pending tick, and a
successful manual retry (failures → 0) flips the mode to `live` and restarts the
chain by itself. The card gains a stalled hint ("Gateway 已停止响应：自动刷新已暂停…
点 retry 重新探测。"), the button says `retry` in that state, and the footnote says
`auto refresh paused` instead of `auto refresh 30s`.

**Measured.** The static showcase is unchanged (CDP read of the latest build still
reports `PREVIEW`, the derived metrics/heading/sentence, ordinals 01–06 / 01–05 /
01–05). The stalled state itself cannot be reached from a static host — it needs a
Gateway that answers and then fails — so its proof is the pure state machine and the
source guard, not a browser run; that is stated rather than implied.

**Tests.** `dashboard/tests/homeRuntime.test.mjs`: the mode table now carries
`reachable + connected + RUNTIME_MAX_FAILURES → stalled` (and the disconnected and
far-past-budget variants), the badge table asserts five distinct badges with `stalled
→ OFFLINE`, the status-label table asserts five distinct labels with stalled matching
`/stopped answering/` and *not* `/connecting/`, a new `pollingActive` truth table, and
the recovery path (`stalled` → successful retry → `live` → polling active). The CTA
table gains `stalled`: dashboard href, "stopped answering" note, not the no-Gateway
note, and a note distinct from both `live` and `connecting`.
`dashboard/tests/homeSurface.test.mjs` pins the wiring: `pollingActive(mode)` is what
gates the loop, the effect deps are `[mode, refreshRuntime]`, the button expression is
`canRefresh && mode !== "stalled" ? "refresh" : "retry"`, the stalled hint exists, and
`shouldStopPolling` no longer appears in the page (one gate, not two).

Twelve negative controls (`.scratch/negative_controls_home.py`, mutate → run →
restore → rerun) all caught, including the three new ones: deleting the `stalled`
branch so the card returns to `SYNCING` forever, making `pollingActive` always true,
and reverting the effect deps to `[probe, refreshRuntime]`.

**Boundary:** like T70–T72, this repository does not deploy the dashboard; the change
reaches `https://uc.jameryw.dev/` only after the operator rebuilds the image (or
re-uploads `dist`), and the deployed CSS filename changes with the build.
