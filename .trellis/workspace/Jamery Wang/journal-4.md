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
