# 72 — The showcase's numbers come from its arrays (T72)

**Symptom.** T71 ended with the same drift class left on the page: the numeric copy was hand-written
next to the arrays it described.

| what the page printed | where it came from | what it claimed |
|---|---|---|
| `<strong>01</strong> orchestration layer` … `<strong>05</strong> execution stages` | literal JSX | 5 = `workflow.length`, but nothing said so |
| `∞` / `24/7` | literal JSX | not counts at all — never checked |
| `Five layers.` | literal string in the heading | `systemLayers.length`, by hand |
| `五个面板对应真实边界：入口、控制、执行、上下文和事件。` | literal string, hand-written list | the five layer names, by hand |
| capability ordinals `01`–`06` | `index: "01"` … on each item | position in `capabilities` |
| layer ordinals `01`–`05` | `index: "01"` … on each item | position in `systemLayers` |
| workflow ordinals `01`–`05` | `step: "01"` … on each item | position in `workflow` |

Three sources of truth for the same number (the array, the summary sentence, the ordinal), so adding a
capability or a workflow stage left the page saying "Five layers" over six items, and an inserted card
could take a borrowed ordinal (`index: "03"` copy-pasted onto the fourth card) with no guard noticing.
This is the T69 shape again: a value with several producers and no vocabulary.

**Root cause.** The numbers were *copy*, not *derivation*. Nothing prevented the arrays from changing
without the prose changing with them, and nothing checked that the ordinal an item carried was its
position.

**Fix.** `dashboard/src/lib/homeRuntime.ts` gained the derivation helpers, each pure and testable:

- `padOrdinal(position)` — 1-based integer → `"01"` … `"12"`, throws `RangeError` for 0, negatives,
  fractions, `NaN`.
- `countWord(count)` / `chineseCount(count)` — `5 → "Five"` / `5 → "五"`, digits past the two small
  tables instead of inventing words.
- `joinChinese(items)` — `["入口","控制","执行","上下文","事件"] → "入口、控制、执行、上下文和事件"`
  (and `["a","b"] → "a和b"`, `["a"] → "a"`, `[] → ""`), so the layer sentence is built from the layer
  titles.
- `showcaseMetrics(workflowSteps)` — the metric row, where only the "execution stages" slot counts
  rendered items; `01`, `∞` and `24/7` are not counts and stay constant.

`dashboard/src/pages/HomePage.tsx` now derives: metrics from `showcaseMetrics(workflow.length)`,
the heading from `countWord(systemLayers.length)`, the layer sentence from
`chineseCount(systemLayers.length)` + `joinChinese(systemLayers.map((layer) => layer.summary))`, and
every ordinal from `padOrdinal(position + 1)`. All 16 hand-written `index` / `step` fields were
deleted, and each layer gained a short `summary` used only by that sentence.

**Measured.** CDP read of the rendered page (`.scratch/read_home_text.mjs`, Edge headless, 1440×900),
before → after the derivation, identical rendering:

```
metrics         01orchestration layer | 05execution stages | ∞worker scale path | 24/7event visibility
heading         Five layers.One execution surface.
sentence        五个面板对应真实边界：入口、控制、执行、上下文和事件。
ordinals        capabilities 01-06 | layers 01-05 | workflow 01-05
```

Then the drift experiment (`.scratch/t72_stage.py apply`): a sixth workflow stage was added to the
source, the bundle rebuilt, and the same probe re-read the page —

```
metrics         01orchestration layer | 06execution stages | ∞worker scale path | 24/7event visibility
heading         Five layers.One execution surface.          (5 layers, correctly unchanged)
ordinals        capabilities 01-06 | layers 01-05 | workflow 01-06
```

The count followed the array with no other edit; the source was then restored byte-for-byte and the
page re-measured back at `05execution stages` / `01-05`.

**Tests.** `dashboard/tests/homeRuntime.test.mjs` (+4): ordinals derived from position (and refusing
0/negative/fraction/`NaN`), counts spelled from the number (`13 → "13"` rather than an invented word),
the Chinese list joins without a dangling or doubled separator, and the metric row's stage slot follows
its argument. `dashboard/tests/homeSurface.test.mjs` (+1): the page source carries no `index: "0X"` /
`step: "0X"` literals, no `<strong>NN</strong>`, no `Five layers.` / `五个面板对应真实边界`, and must
route through `showcaseMetrics(workflow.length)`, `countWord(systemLayers.length)`,
`chineseCount(...)` + `joinChinese(...)`, at least three `padOrdinal(` calls, and `key=` on the title.

Nine negative controls (`.scratch/negative_controls_home.py`, mutate → run → restore → rerun) all
caught: the T70 overflow pin, `.home-page` as its own scroller, `enabled: true`, the `nth-child` nav
hiding, every CTA hardcoded to `#/dashboard`, the false "live Gateway" note, the metric row hardcoding
five stages, the heading hardcoding the layer count, and a capability carrying its own ordinal.

**Boundary:** like T70 and T71, this repository does not deploy the dashboard — the change reaches
`https://uc.jameryw.dev/` only after the operator rebuilds the image (or re-uploads `dist`), and the
deployed CSS filename changes with the build, which makes the redeploy checkable in one request.
