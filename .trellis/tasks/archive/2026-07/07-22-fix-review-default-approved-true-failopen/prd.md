# fix-review-default-approved-true-failopen

## Goal

`reviewSubtaskLocal` and `reviewSubtaskRemote` (`orchestrator.ts`) default `approved` to **true** when the LLM review output is missing the `approved` key (`parsed.approved ?? true`) or fails JSON parse entirely (`catch { return { approved: true, ... } }`). A malformed/empty/incomplete supervisor review silently APPROVES a subtask — fail-open on a quality gate. The rejected-subtask branch (`if (st.review && !st.review.approved)`, line 2106) is skipped, so a possibly-defective subtask passes review.

Same anti-pattern PR #347 fixed in gRPC (`subtask_completed success defaulted true`). A review gate must fail CLOSED (reject) on missing/invalid verdict, not open.

## What I already know

* `reviewSubtaskLocal` (line ~1820-1829): `JSON.parse(result.output)` → `approved: parsed.approved ?? true`; `catch { return { approved: true, issues: [], suggestions: [] } }`.
* `reviewSubtaskRemote` (line ~1885-1894): identical pattern — `parsed.approved ?? true`; `catch { return { approved: true, ... } }`.
* Line 2106: `if (st.review && !st.review.approved)` — rejection handling. `approved: true` default skips this.
* LLM review schema (line 1796): `approved: { type: "boolean" }` — declared, not required. Prompt (lines 2181-2182) documents `approved: boolean (true if satisfactorily completed)`. A correct LLM always emits `approved`; default-true only masks LLM/parsing failure.
* No direct test covers the parse path (grep found none in *.test.ts). Latent.
* PR #347 precedent (gRPC subtask_completed success default true — fail-open fixed to fail-closed).
* Spec: `error-handling.md` — never silently suppress; fail-safe defaults.

## Root cause

`?? true` + `catch → true` treats "couldn't read the verdict" as "verdict = approve." A quality gate must treat unreadable/missing verdict as "reject."

## Requirements

* Missing `approved` key → reject (default `false`), not approve.
* JSON parse failure → reject (`approved: false`) + log warning with context, not silent approve.
* Both `reviewSubtaskLocal` and `reviewSubtaskRemote` fixed identically.
* Valid `approved: true` / `approved: false` from a well-formed response — unchanged.
* On parse failure, surface a reason in `issues` so the rejection is debuggable.

## Acceptance Criteria

* [ ] Well-formed `{approved: true}` → approved (unchanged).
* [ ] Well-formed `{approved: false}` → rejected (unchanged).
* [ ] Missing `approved` key → rejected (was approved).
* [ ] Unparseable output → rejected + warning logged + issue recorded (was silent approve).
* [ ] Both local + remote paths covered by unit tests.
* [ ] Existing scheduler.test.ts review test still passes.

## Definition of Done

* Tests added/updated.
* `bun test` (or project TS test runner) green; tsc -p tsconfig.app clean (per memory [[dashboard-ci-tsc-noop]] — use the app tsconfig, not the noop project-ref one).
* CI green.

## Technical Approach

Both paths, same change:
```ts
try {
    const parsed = JSON.parse(...);
    return {
        approved: parsed.approved === true,   // fail-closed: anything but explicit true rejects
        issues: parsed.issues ?? [],
        suggestions: parsed.suggestions ?? [],
    };
} catch (err) {
    this.pi.logger.warn("Supervisor review output unparseable — rejecting (fail-closed)", err);
    return { approved: false, issues: ["Review output could not be parsed"], suggestions: [] };
}
```

`parsed.approved === true` (strict) is stricter than `?? false` — rejects `approved: "true"` (string) or other truthy junk too. Matches "explicit true only."

## Decision (ADR-lite)

**Context**: review gate fail-open on missing/invalid verdict.
**Decision**: strict `=== true` approval; parse failure → reject + log. Fail-closed.
**Consequences**: a buggy LLM that omits `approved` now causes rejection (visible, debuggable) instead of silent approval. Correct LLMs unaffected. Slightly higher rejection rate on malformed output — desirable for a quality gate.

## Out of Scope

* Changing the LLM review prompt (already documents `approved`).
* Making `approved` required in the zod/schema (runtime LLM output, not zod-validated here).
* The remote polling timeout path (separate concern — what happens when review never completes).

## Technical Notes

* File: `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` lines ~1820-1829 (local), ~1885-1894 (remote).
* Test: new file or extend an existing `*.test.ts` — check for an orchestrator review test file.
* Related: PR #347 (gRPC success default), [[dashboard-ci-tsc-noop]] (tsc verification method).
