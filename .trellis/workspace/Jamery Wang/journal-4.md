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
