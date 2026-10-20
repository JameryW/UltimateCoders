# Jamery Wang — Journal 4

> Continued from `journal-3.md` (1968 lines). Started when the active file
> crossed the 2000-line threshold.

---

## Session 68: The compile trail becomes visible (P1b follow-on)

**Date**: 2026-10-19
**Task**: Dashboard panel for the context-compile trail + wire the frontend
test suite into CI
**Branch**: `feat/context-manifest-panel`

### Summary

The manifest trail has been writable (#735) and readable over HTTP
(#739, #741), but an operator still had to leave the dashboard and curl
the endpoint to see it. The task detail is where they already look to
understand what a task did. While wiring the panel in, the second half of
the same problem surfaced: the `.test.mjs` suite had **no CI job at all**
— five test files, run by hand only. A test that never runs is not
evidence, so the panel's tests would have been exactly that.

### Main Changes

- `dashboard/src/lib/contextManifests.ts` — pure helpers: key parsing on
  the **last two** colons (so `task:10:s1:3` keeps its task id), the
  task-scan order `((task:subtask, numeric attempt))`, and formatters that
  read only known summary fields.
- `dashboard/src/components/panels/ContextManifestPanel.tsx` — pure
  `ContextManifestList` (no effects, SSR-testable) + `ContextManifestTrail`
  fetch wrapper on the same `/dashboard/api/*` + stored-token pattern as
  metrics/alerts. Task detail mounts it after the subtask DAG.
- `ci-dashboard.yml` gains a `node test` job (Node 22 — the tests import
  `.ts` sources directly; Node 20 cannot). README CI tables in both
  languages updated so the "Checks" cell matches.
- Guard pins, measured after `git add`: line-endings
  `(2069,2060)` → **`(2072,2063)`** (three new text files); spec-refs
  census `342` → **`343`**. Footnote recorded in the pin comment: the
  panel's `.mjs` test is **invisible** to the census — the mention regex
  has no `mjs` suffix, so that citation is not counted at all.

### Git Commits

| Hash | Message |
|------|---------|
| `88970ea` | feat(dashboard): render a task's context-compile trail in the task detail |
| `b0b16e0` | Merge pull request #743 from JameryW/feat/context-manifest-panel |

### Testing

- [OK] 10 new tests (order incl. `s1`/`s10` and 0/2/10, key parsing with a
  colon-bearing task id, headline, poison fields, tier order, drop
  aggregation, SSR order + empty state); full suite **22 passed**
- [OK] Mutations one at a time, each restored byte-identically (sha256
  compared): A sort by string → 2 red (unit + SSR order); B parse on the
  first colon → 1 red; C headline drops the drop count → 1 red; D panel
  dumps the raw summary → 1 red (the "no content" render assertion)
- [OK] `tsc` (app + node) and `eslint` clean
- [OK] PR #743 CI: all 5 workflows SUCCESS; the new `node test` job logged
  `# tests 22 / # pass 22` — a real run, not a skip
- [NOTE] Opening journal-4.md itself moved two corpus pins in
  `test_check_journal_ledger.py` that assumed this ledger holds exactly
  3 files (`"this ledger: 3 file(s)"` and `len(ours) == 3`) — both now 4,
  cause recorded. Standing lesson: a new artefact moves pinned corpus
  counts, so grep who pins it **first**. The ledger guard also only reads
  git-tracked journals, so the "index says 68, journals hold 67" advisory
  was an artefact of the untracked new file and cleared at `git add`.
- [NOTE] The new file was written LF while the workspace is CRLF; the
  ledger guard flagged it and it was converted before commit.
- [NOTE] `vite build` cannot run in this local checkout: `tailwindcss` is
  resolvable only from inside `@tailwindcss/vite`'s pnpm-nested
  node_modules. Reproduced **identically at HEAD** before the diff
  existed (2262 modules, same resolve error) ⇒ local install shape, not
  the change. CI's fresh `vite build` passed, which is the authority.
- [NOTE] Mutation restores go through Python text mode, which normalises
  line endings to CRLF on this checkout. Both restored sources are
  uniformly CRLF (verified per file by byte count), so no mixed-endings
  violation — but the "byte-identical" claim for those two files is
  really "byte-identical modulo the newline translation the restore
  itself performs", and the sha256 comparison is what caught that.

### Status

[OK] **Completed**

### Next Steps

- Remaining optional follow-on: inference-side consumption of
  `render_parts()` — still no in-repo path (`llm.py` serves planning/Q&A
  only; the coding loop lives in an external CLI subprocess). This needs
  the external adapter to consume the split, so it cannot be closed from
  inside this repository.
- #656 stays open (market scheduling blocked on the external plan §21)

---

## Session 69: The census could not see whole classes of citations

**Date**: 2026-10-19
**Task**: Teach the spec-reference guard the `.mjs` / `.jsonl` extensions
it was silently ignoring
**Branch**: `fix/spec-refs-mjs-jsonl`

### Summary

The previous session's own footnote was the lead: "`.mjs` 提及对 spec-refs
普查隐形". Taken seriously, that is not a footnote — it is a checker that
reports green over a class of citations it cannot see. Measured before
touching anything: of all backticked path-like spans in the spec corpus,
exactly two real repo extensions were invisible — `mjs` (the dashboard
test suite) and `jsonl` (the Trellis ledgers). Everything else the regex
missed was either a dotted identifier (`pytest.raises`), a foreign binary
(`wsl.exe`) or an operator-supplied path (`docker/.env`).

### Main Changes

- `mjs` and `jsonl` added to all four places that answer "is this a path":
  `CODE_EXT` (basename index), `REF_RE` (`path:line` pointers),
  `PATH_SPAN_RE` (quoted-span predicate), `MENTION_PATH_RE` (citations).
- Corpus effect measured, not assumed: mentions `343 → 345`, both resolved;
  `path:line` counts unchanged (130 scanned / 121 located) so those pins
  did not move.
- Three synthetic tests: the two extensions count as mentions; `x.mjs:3`
  enters the line-checked pipeline; **`lock` stays out** — prose quotes
  `task_store.lock`, and adding it would manufacture a phantom mention.
  The exclusion is pinned, not left to a comment.

### Git Commits

| Hash | Message |
|------|---------|
| `2f578b6` | fix(guard): let the spec-reference census see .mjs and .jsonl citations |
| `0ddede5` | Merge pull request #745 from JameryW/fix/spec-refs-mjs-jsonl |

### Testing

- [OK] Mutations one at a time, sha256-restored: A drop `mjs|jsonl` from
  `MENTION_PATH_RE` → exactly the synthetic mention test **and** the real
  census pin red (same rule at two layers); B drop from `REF_RE` → exactly
  the line-pointer test red; C add `lock` → exactly the non-addition test
  red. `48 passed` after each restore.
- [OK] PR #745 CI: Scripts + Python both SUCCESS; the three new tests
  logged `PASSED` individually and the suite `48 passed`.
- [OK] On merged main: seven guards green, census 345, `48 passed`.
- [NOTE] **The most instructive failure of the session was my own edit.**
  The first change updated the comment above `MENTION_PATH_RE` and left the
  regex untouched — the prose then claimed a fix the code did not have. It
  was the `345` census pin that went red and exposed it: the exact failure
  mode this guard exists for, reproduced in the commit that fixes it.
- [NOTE] Mutation scripts got double-executed by the sandbox again (the
  recorded trap): the first run mutates, the second run's pre-assert dies
  and the visible output looks like "nothing happened" while the file is
  already changed. Fixed the way the memory prescribes — idempotent
  preconditions, and judgment only from the disk (`sha256`, `grep`).

### Status

[OK] **Completed**

### Next Steps

- Unchanged: inference-side consumption of `render_parts()` (needs the
  external CLI adapter), and #656 (blocked on the external plan §21).

## Session 70: The same blind spot, one guard over

**Date**: 2026-10-19
**Task**: Teach `check-workflow-inputs.py` the `.js` / `.jsonl` / `.mjs` /
`.proto` extensions its extractor could not see
**Branch**: `fix/workflow-inputs-suffixes`

### Summary

Session 69 fixed one instance of "a checker that cannot see a class of
paths reports green"; this session found the same class one guard over and
closed it. `check-workflow-inputs.py` decides whether a `run:` step's named
inputs are covered by that workflow's `paths` (the T35 rule), but
`FILE_SUFFIXES` knew neither `.js`, `.jsonl`, `.mjs` nor `.proto` while
tracked files of those shapes exist (1 / 633 / 9 / 1). A run step naming
one extracted nothing, so judgment 2/3 could never fire for it.

Measured before touching anything: zero run bodies name any of the four
today, so the corpus summary moves `21 -> 21` and the real-repo pin is
insensitive to this class on purpose — the teeth are synthetic rows, and
the comment says exactly that instead of implying a corpus effect.

While writing the pin: the guard's header already claimed "the test pins
its contents" about `TOP_LEVEL_DIRS`, and no test did. Prose promising a
pin that does not exist is the #745 failure mode one layer up — so
`test_the_declared_tables_are_pinned` was added and the promise became
true for both declared tables.

### Main Changes

- `js`, `jsonl`, `mjs`, `proto` added to `FILE_SUFFIXES`. Corpus effect
  measured, not assumed: summary stays `21 run-step references / 13
  subject to coverage`.
- Four positive extractor rows (real repo shapes: the dashboard `.mjs`
  suite, a Trellis `.jsonl` ledger, the ESLint `.js` config, the gRPC
  `.proto` contract) and three negative rows pinning the deliberate
  non-additions by their phantom shapes (`scripts/build.lock`,
  `vendor/bin/protoc.exe`, `docker/config/.env`).
- New `test_the_declared_tables_are_pinned`: `TOP_LEVEL_DIRS` and
  `FILE_SUFFIXES` compared as ordered tuples — the header's claim about
  pinning now has a test behind it.
- The `node tools/build.js` row annotated: its negative reason moved from
  "js is not a suffix" to "tools/ is not a top-level dir".
- `lock` / `exe` / `env` stay out; `jsx` / `sql` stay unregistered (0
  tracked files each) under the same registration rule as
  `TOP_LEVEL_DIRS`.

### Git Commits

| Hash | Message |
|------|---------|
| `454dd03` | fix(guard): let the workflow-inputs extractor see .js/.jsonl/.mjs/.proto |
| `d8a1fc2` | Merge pull request #747 from JameryW/fix/workflow-inputs-suffixes |

### Testing

- [OK] Mutations one at a time, sha256-restored (8/8, each red at its own
  assertion row): A-D drop each of `js`/`jsonl`/`mjs`/`proto` → exactly
  the matching positive row red; E-G add `lock`/`exe`/`env` → exactly the
  matching negative row red; H drop `vendor` from `TOP_LEVEL_DIRS` →
  exactly the new declared-tables pin red.
- [OK] PR #747 CI: Python + Scripts + Workflow Inputs all SUCCESS
  (`completed/success`, 0 fail). `test_check_workflow_inputs.py`
  `7 passed` (was 6) on both Python 3.9 and 3.12 legs.
- [OK] Seven guards green on merged main; spec-refs census `345` and
  line-endings `(2073, 2064)` unchanged — no new files.

### Status

[OK] **Completed**

### Next Steps

- Unchanged: inference-side consumption of `render_parts()` (needs the
  external CLI adapter), and #656 (blocked on the external plan §21).
## Session 71: A "most recent N" that was not recent

**Date**: 2026-10-20
**Task**: Make the manifest recency read honest — `recent()` /
`order=recent` is a time window, not a key window
**Branch**: `feat/manifest-recency-window`

### Summary

The recon that opened this session found the defect before any code was
written: `RuntimeState.query` ends `ORDER BY record_key LIMIT n`, so the
truncation happens in *key* order. A "fetch N, then sort by `recorded_at`"
implementation therefore answers "the newest of the first N keys" — and
drops exactly the record a recency read exists to surface: the one whose
key sorts last while its `recorded_at` is newest. Calling that "the most
recent N" would have been a lie with a correct-looking sort on top.

`ContextManifestOutbox.recent()` walks the whole `task:`/`task:subtask:`
prefix on the key cursor (`SCAN_PAGE` per `state.query` call) and truncates
only *after* the time sort: `limit` bounds the response, never the scan.
The documented total order is `(-recorded_at, key)` — newest first, ties by
key ascending — pinned with a deliberately non-key-ordered, tie-carrying
window so a dropped tie-break cannot hide behind Python's stable sort. The
dashboard endpoint gains `order=key|recent` (default `key`, existing
behaviour untouched; anything else is a 400 "Invalid order").

The doc note names `tests/python/test_context_outbox.py` a second time in
§6, so the spec-refs mentions census moved `345 -> 346` and the pin moved
in the same commit. Honest re-measurement also corrected a stale memory
baseline: collect-only is `1964 -> 1972` (+8 tests), not "1960 + 8" — the
old `1960` was measured before earlier merges and should not have been
carried forward unchallenged.

### Main Changes

- `ContextManifestOutbox.recent(task_id, *, subtask_id, limit)` — key-cursor
  scan of the whole prefix, truncate after the time sort; same strict limit
  validation as `query` (1..1000, `type is int` so `True` is rejected);
  trailing-`:` boundary preserved (`task-1` never swallows `task-10`).
- `GET /dashboard/api/context/manifests?order=recent` — wired to `recent()`
  (M6 in the battery pins the wiring itself).
- `docs/agent-os-architecture.md` §6 documents the recency read and why
  sort-after-fetch is a fake "most recent N".
- Spec-refs mentions pin `345 -> 346`; line-endings `(2073, 2064)` untouched
  (no new files).

### Git Commits

| Hash | Message |
|------|---------|
| `ca78cd8` | feat(context): manifest recent() is a time window, not a key window |
| `05b2471` | Merge pull request #749 from JameryW/feat/manifest-recency-window |

### Testing

- [OK] Mutations one at a time, sha256-restored (8/8, each red at its own
  assertion row, red sets pairwise distinct): M1 naive fetch-limit-then-sort
  (the headline defect) → the two time-window pins and the endpoint pin red;
  M2 drop key tie-break → exactly the tie-pin red; M3 sort ascending → the
  five order-sensitive pins red; M4 drop colon boundary → exactly the scope
  pin red; M5 drop limit validation → exactly the limits pin red; M6 `recent`
  wired to `history` → exactly the endpoint time-window pin red; M7 accept
  any `order` → exactly the unknown-order pin red; M8 ignore subtask scope →
  exactly the endpoint scope pin red.
- [OK] PR #749 CI: Python + Scripts + Dashboard all SUCCESS
  (`completed/success` on headSha `ca78cd83`, 11 checks, 0 fail) on both
  Python 3.9 and 3.12 legs.
- [OK] Seven guards green on merged main; spec-refs census `346` (pin moved
  in the same commit as the doc note) and line-endings `(2073, 2064)`
  unchanged — no new files. Collect-only measured `1964 -> 1972` (+8).

### Status

[OK] **Completed**

### Next Steps

- Unchanged: inference-side consumption of `render_parts()` (needs the
  external CLI adapter), and #656 (blocked on the external plan §21).

## Session 72: A "N recorded" that was not N

**Date**: 2026-10-20
**Task**: Make the history side honest — `history`/`task_history` are
oldest-key windows, and `count`/the panel label must not present a window
as the whole trail
**Branch**: `feat/window-vs-total`

### Summary

Session 71 fixed the recency read; this session closes its mirror image on
the history side. `history()`/`task_history()` truncate in *key* order, so
when the trail is longer than `limit` the window holds the **oldest**
records and silently drops the newest attempts — while the docstring said
"Every recorded compile" and the endpoint returned `count ==
len(manifests)`, which the panel printed as "N recorded". The reported
number and the thing it claimed to count were not the same source (law 3):
`count` is a window size, and displaying it as a total understates the
trail exactly when the trail is long enough to truncate.

`ContextManifestOutbox.total()` walks the whole prefix on the key cursor
(same `SCAN_PAGE` pagination as `recent()`) and returns the scan count —
what exists in scope, not what came back. The endpoint response gains
`"total"` beside `"count"` (computed inside the try block, so a scan
failure is a 503 via `_unavailable` like the rest of the endpoint). The
panel's count line goes through a new pure `manifestCountLine(shown,
total)`: "1 of 3 recorded" when truncated, "3 recorded" when the window is
the whole trail, and "2 recorded" with **no invented "of N"** when `total`
is absent or non-finite — degrade honestly, never fabricate the missing
denominator. The docstrings now say what the functions actually do: "Up to
``limit`` recorded compiles… Truncation is in key order, so when the trail
is longer than ``limit`` the window holds the *oldest* records and drops
the newest attempts".

The §6 note names `dashboard/tests/contextManifests.test.mjs` a second
time, so the spec-refs mentions census moved `346 -> 347` and the pin moved
in the same commit. Tests were split until every failure mode had its own
red set (law 2's "打红同一集合 ⇒ 一条是装饰"): history and task_history got
separate window-direction pins, the scope pin uses `sa`/`sb` (no shared
prefix) so a colon-boundary mutation cannot also redden it, the colon
boundary is its own pin, and the mjs count line is two pins — truncation
vs degradation. The scan-vs-window pin is sized to **101 records** so the
scan (101) diverges observably from the default window (100); a "reuse the
window" mutation would not redden a 3-record pin.

### Main Changes

- `ContextManifestOutbox.total(task_id, *, subtask_id)` — full key-cursor
  scan count (`SCAN_PAGE` per page, colon boundary kept); the scan count,
  not a window.
- `history`/`task_history` docstrings corrected from "Every recorded
  compile" to the window semantics they actually have (oldest-key
  truncation).
- `GET /dashboard/api/context/manifests` response gains `"total"` (inside
  the try block → 503 on failure), `"count"` stays the window size.
- `manifestCountLine(shown, total)` in `dashboard/src/lib/contextManifests.ts`
  with the degrade path; `ContextManifestPanel.tsx` tracks `total` state
  and uses it for the header label.
- Spec-refs mentions pin `346 -> 347`; line-endings `(2073, 2064)`
  untouched (no new files).

### Git Commits

| Hash | Message |
|------|---------|
| `c10bf3b` | fix(context): report the scan total beside the window (Session 72) |
| `88b9429` | Merge pull request #751 from JameryW/feat/window-vs-total |

### Testing

- [OK] Mutations one at a time, sha256-restored (10/10, each red at its own
  assertion row, red sets pairwise distinct, extra-red empty everywhere):
  M1 total-as-window-count → exactly `test_total_counts_the_scan_not_the_window`
  (the 101-record pin); M2 single-page scan → exactly
  `test_total_walks_past_one_query_page`; M3 history flipped to a newest
  window → exactly `test_history_limit_is_an_oldest_key_window`; M4 same on
  `task_history` → exactly `test_task_history_limit_is_an_oldest_key_window`
  (split so M3/M4 don't share a red set); M5 total ignores subtask scope →
  `{test_total_scopes_to_one_subtask, test_total_respects_the_subtask_scope}`;
  M6 colon boundary dropped → exactly `test_total_keeps_the_colon_boundary`;
  M7 endpoint `total = len(manifests)` → exactly
  `test_total_is_the_scan_count_not_the_window`; M8 endpoint drops scope →
  exactly `test_total_respects_the_subtask_scope`; M9 mjs always-"N
  recorded" → exactly the truncation pin; M10 mjs drops the typeof guard →
  exactly the degradation pin.
- [OK] PR #751 CI: Python + Scripts + Dashboard all `completed/success` on
  headSha `c10bf3b6` (14 checks, 0 fail), both Python 3.9 and 3.12 legs.
- [OK] Six guards green: spec-refs 48 (mentions `347`, moved in the same
  commit as the doc note), line-endings 5 (`(2073, 2064)` unchanged),
  journal-ledger 27, tasks-refs 14, workflow-inputs 7, readme-ci-table 4;
  ruff clean on `python/ tests/`. Affected suites: `test_context_outbox.py`
  20 pass, `test_dashboard_context_manifests.py` 15 pass,
  `contextManifests.test.mjs` 12 pass. Collect-only measured `1972 -> 1980`
  (+8 = 6 outbox + 2 dashboard tests) on the merged tree.

### Status

[OK] **Completed**

### Next Steps

- Unchanged: inference-side consumption of `render_parts()` (needs the
  external CLI adapter), and #656 (blocked on the external plan §21).

## Session 73: The attempt in the key was a constant

**Date**: 2026-10-20
**Task**: Make the audit trail's attempt-scoped manifest key real — the only
production writer hardcoded attempt `0`
**Branch**: `fix/manifest-attempt-identity`

### Summary

The manifest outbox is attempt-scoped on purpose: its module docstring
promises "a manifest is a historical snapshot, so if a retry ever
recompiles, both snapshots must survive", and `manifest_key` puts the
attempt in the key. But the **only production writer** called
`manifest_key(parent, id, 0)` while every other attempt identity it stamps
(`_delivery_identity`, the checkpoint key, `attempt_id` on events, the
execution envelope) comes from `subtask.dispatch_retry_count`. So a
re-dispatch wrote the **same key** as the first attempt, `record()`
replaced the earlier attempt's summary in place — including its
`recorded_at` — so the first attempt's snapshot (content and time) was
gone. (This sentence originally said the first `recorded_at` was kept: it
was quoted from `record()`'s docstring without running it. Session 74
measures the sentence, finds it false, and corrects the prose — the old
attempt's snapshot was gone entirely, not half-preserved.) Net effect in
production: at
most one manifest per subtask, the attempt component dead, and the
"attempt" column the panel and the endpoint sort by could only ever read
`0`. Sessions 71/72's recency and scan-count reads were correct about a
trail whose identity scheme never existed.

The writer now keys on `subtask.dispatch_retry_count` — the same identity
the checkpoint and terminal-delivery keys use. The in-process
`retry_count` is deliberately **not** the identity: the compiler runs once
per dispatch (before the retry loop), so a re-dispatch must land beside
the earlier snapshot. Rows written before this change are genuinely
attempt 0, so no migration is needed. Two worker-level pins: a non-zero
dispatch attempt lands in the key (with a different `retry_count` set, so
the wrong field cannot pass), and two dispatches keep both snapshots
(`total("t1") == 2`).

The writer's docstring grew by eight lines, which moved the
`_save_checkpoint` definition `1457 -> 1465`; the spec-refs
repaired-reference pin and the `hook-guidelines.md:109` cited range were
re-measured together (`1465-1519`) in the same change. Mentions census
stays `347` (no new path citation) and line-endings `(2073, 2064)` is
unchanged (no new tracked files).

### Main Changes

- `worker._record_context_manifest` — key the snapshot on
  `dispatch_retry_count`, the same attempt identity as the checkpoint and
  terminal-delivery keys; record the reason in the docstring.
- `context_outbox.manifest_key` — state which attempt the parameter is
  (dispatch attempt, not the in-process retry counter).
- `docs/agent-os-architecture.md` §6 — the manifest key's attempt is the
  dispatch attempt; the only writer used to hardcode it away, so
  "attempt-scoped history" never happened in production.
- `tests/python/test_context_outbox.py` — two worker-level pins
  (non-zero attempt carried; both snapshots survive a re-dispatch).
- Spec-refs repaired-reference pin `_save_checkpoint` `1457 -> 1465`; the
  `hook-guidelines.md:109` citation re-measured to `1465-1519`.

### Git Commits

| Hash | Message |
|------|---------|
| `c62d328` | fix(context): key the manifest on the dispatch attempt (Session 73) |
| `99efcfb` | Merge pull request #753 from JameryW/fix/manifest-attempt-identity |

### Testing

- [OK] Mutations one at a time, sha256-restored, judgment only from disk
  (2/2 `ALL_OK`, red sets pairwise distinct, assertion-red, extra-red
  empty): M1 restore the hardcoded `0` → exactly
  `{test_worker_records_the_manifest_under_the_dispatch_attempt,
  test_a_redispatch_keeps_the_earlier_attempts_manifest}`; M2 key on the
  in-dispatch `retry_count` → exactly
  `test_worker_records_the_manifest_under_the_dispatch_attempt` (the
  second pin sets `retry_count == dispatch_retry_count` on purpose so M2
  cannot redden it). Pristine sha256 `73d61563…` restored and re-verified
  from a separate process.
- [OK] PR #753 CI: Python CI + Scripts CI both `completed/success` on
  headSha `c62d3280` (11 checks, 0 fail; the two Python legs took
  9m42s/10m15s and `test (Python 3.9)` passed).
- [OK] Seven guards green on this tree: spec-refs 48 tests (audit
  `112 ok / 9 stale / 9 ambiguous`, mentions `347`), tasks-refs
  `883 ok / 0 dangling` + selftest, line-endings `(2073, 2064)`
  unchanged, workflow-inputs `21 refs / 13 covered`, readme-ci-table
  `11 workflows`, journal-ledger 27, codex-flow; `ruff check scripts/`,
  `python/`, `tests/` clean; py39 compat scan `165 files, 0 problems`.
- [OK] Affected suites: `test_context_outbox.py` **22 passed** (was 20),
  `test_dashboard_context_manifests.py` 15 passed,
  `test_context_compiler.py` 29 passed. Collect-only measured
  `1980 -> 1982` (+2 = the two new pins).

### Status

[OK] **Completed**

### Next Steps

- Unchanged: inference-side consumption of `render_parts()` (needs the
  external CLI adapter), and #656 (blocked on the external plan §21).

## Session 74: A timestamp quoted from prose

**Date**: 2026-10-20
**Task**: Measure `record()`'s timestamp contract instead of quoting it —
the docstring promised the first-seen time, the code re-stamps
**Branch**: `fix/manifest-timestamp-claim`

### Summary

Session 73's entry claimed `record()` "kept the first `recorded_at`" when a
re-dispatch overwrote an attempt's snapshot. That sentence was never
measured: it was read off `record()`'s docstring ("Either way the stored
record is the summary plus the first time it was seen") and copied into the
PR body and this journal. Executed as a test, the docstring's own promise
fails — `assert 5000.0 == 1000.0`: the differing-content branch returns
`{"summary": summary, "recorded_at": time.time()}`, i.e. the time of the
content that survived.

The behaviour is the coherent contract, so the prose is what was fixed.
`recent()` orders by `recorded_at` and claims "the newest compiles by
recorded_at"; a same-attempt recompile (a redelivered subtask that executes
again) replaces the earlier snapshot, so keeping the first clock would file
the newest content under an older time and hide it from the recency read.
The module docstring's "Every `compile_context` call leaves one snapshot"
was equally false and now states the real contract: one snapshot per
compile identity, a same-attempt recompile replaces it, and the replaced
snapshot is not recoverable. The Session 73 entry above is corrected in
place (with the provenance of the wrong claim), and PR #753's body carries
the same correction.

Two pins freeze the measured behaviour: the write side (a differing rewrite
moves `recorded_at`; an identical rewrite keeps it) and the read side
(`recent()` surfaces the recompiled record as the newest). The docstring's
promise was also executed as a test before being rejected — the red run is
this session's evidence, not an argument.

### Main Changes

- `context_outbox.record` docstring — an identical rewrite is a no-op
  **and keeps** the time; a differing rewrite replaces the record **and
  re-stamps** `recorded_at`; the old "first time" wording is named as
  wrong.
- Module docstring — one snapshot per compile identity
  (`task:subtask:attempt`); a same-attempt recompile replaces the earlier
  snapshot, and `recorded_at` belongs to the summary that survives.
- `docs/agent-os-architecture.md` §6 — the same statement beside the
  per-attempt snapshot semantics.
- `tests/python/test_context_outbox.py` — two pins
  (`test_repeat_record_with_different_summary_moves_its_recorded_at`,
  `test_recent_surfaces_the_latest_recompile_as_the_newest`); the existing
  no-op pin already froze the other branch.
- Session 73's journal sentence corrected in place; PR #753's body
  corrected the same claim.

### Git Commits

| Hash | Message |
|------|---------|
| `7cbd7d8` | fix(context): let the manifest timestamp follow its content (Session 74) |
| `8950982` | Merge pull request #755 from JameryW/fix/manifest-timestamp-claim |

### Testing

- [OK] The docstring's promise executed as a test before being rejected:
  `assert 5000.0 == 1000.0` red on the tree that claimed first-seen; then
  replaced by the two pins above (behaviour, not prose, is the contract).
- [OK] Mutations one at a time, sha256-restored, judgment only from disk
  (3/3 `ALL_OK`, red sets pairwise distinct, assertion-red, extra-red
  empty): M1 replacement keeps the earlier `recorded_at` → exactly
  `{test_repeat_record_with_different_summary_moves_its_recorded_at,
  test_recent_surfaces_the_latest_recompile_as_the_newest}`; M2 an
  identical rewrite re-stamps → exactly
  `test_repeat_record_with_same_summary_is_a_no_op`; M3 a differing
  rewrite returns the stale record → exactly
  `{test_repeat_record_with_different_summary_keeps_the_latest,
  test_repeat_record_with_different_summary_moves_its_recorded_at,
  test_recent_surfaces_the_latest_recompile_as_the_newest}`. Pristine
  sha256 `de5cc0b7…` restored and re-verified from a separate process.
- [OK] PR #755 CI: Python CI + Scripts CI both `completed/success` on
  headSha `7cbd7d8d` (11 checks, 0 fail; Python legs 7m23s/9m14s).
- [OK] Seven guards green on this tree: spec-refs 48 tests (audit
  `112 ok / 9 stale / 9 ambiguous`, mentions `347` — unchanged),
  tasks-refs `883 ok / 0 dangling` + selftest, line-endings
  `(2073, 2064)` unchanged (no new tracked files), workflow-inputs
  `21/13`, readme-ci-table `11 workflows`, journal-ledger 27, codex-flow;
  `ruff check scripts/`, `python/`, `tests/` clean; py39 compat scan
  `165 files, 0 problems`.
- [OK] Affected suites: `test_context_outbox.py` 24 passed (was 22),
  `test_dashboard_context_manifests.py` 15. Collect-only measured
  `1982 -> 1984` (+2 = the two new pins).

### Status

[OK] **Completed**

### Next Steps

- Unchanged: inference-side consumption of `render_parts()` (needs the
  external CLI adapter), and #656 (blocked on the external plan §21).
