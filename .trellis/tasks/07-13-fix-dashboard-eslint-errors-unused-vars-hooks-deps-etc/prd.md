# Fix Dashboard ESLint Errors

## Background

`./test-all.sh` passes, but `dashboard npm run lint` shows 28 errors across
~9 files (engine_pb.ts excluded — generated). CI doesn't gate on lint
(dashboard checks pass), but these are real code-quality issues. User asked
to "完整测试一遍，解决存在的问题" — fix them.

## Error categories (verified this session)

| Count | Rule | Type | Risk |
|-------|------|------|------|
| 7 | refs during render (useRef .current in render) | React anti-pattern | HIGH — refactor |
| 5 | @typescript-eslint/no-unused-expressions | mechanical | LOW |
| 4 | @typescript-eslint/no-unused-vars (_grpcDisconnect/_reason/_skipped/_dashGrpcDisconnect) | mechanical | LOW |
| 3 | react-refresh/only-export-components | export structure | LOW — file split or eslint-disable |
| 3 | setState synchronously in effect | React anti-pattern | HIGH — refactor |
| 1 | prefer-const (components never reassigned) | mechanical | LOW |
| 1 | no-case-declarations (lexical decl in case) | mechanical | LOW |

## Decisions (locked)

- **D1**: Fix the LOW-risk mechanical errors first (unused-vars,
  no-unused-expressions, prefer-const, no-case-declarations). These are
  safe, no behavior change. For unused-vars with `_` prefix (intentionally
  unused destructured vars): eslint-disable-next-line or remove if truly
  unneeded (check usage).
- **D2**: react-refresh/only-export-components — these files export both
  components and constants/functions. Add `// eslint-disable-next-line
  react-refresh/only-export-components` where splitting the file is
  disproportionate (it usually is for a single constant). LOW risk.
- **D3 (DEFERRED)**: refs-during-render (7) + setState-in-effect (3) are
  React anti-patterns requiring careful refactor (move ref access into
  effects/callbacks, restructure setState timing). These can introduce
  behavior changes and need runtime testing. **Out of scope for this PR** —
  separate task with careful review. Leave them as-is (pre-existing) and
  document in PRD that they're deferred.
- **Scope**: D1 + D2 only. ~14 errors fixed (mechanical + eslint-disable for
  react-refresh). The 10 React anti-pattern errors (refs/state) deferred.

## Acceptance Criteria

- [ ] `npm run lint` error count drops from 28 to ≤14 (the deferred
      refs-in-render + setState-in-effect remain).
- [ ] No behavior change (mechanical fixes + eslint-disable comments only).
- [ ] `npx tsc --noEmit` still green.
- [ ] `bun test` (dashboard, if exists) still green.

## Technical Approach

1. Run `npm run lint` to get the full error list with file:line.
2. For each mechanical error: read the line, apply the minimal fix:
   - unused-vars `_X`: if truly unused, eslint-disable-next-line
     @typescript-eslint/no-unused-vars (they're destructured for position).
   - no-unused-expressions: wrap in void or restructure (check context).
   - prefer-const: change `let` to `const`.
   - no-case-declarations: wrap case body in `{}`.
3. react-refresh: eslint-disable-next-line for the export line.
4. Do NOT touch refs-in-render / setState-in-effect (deferred).
5. Verify: lint count, tsc, test.

## Risk

- **Low**: mechanical fixes + eslint-disable. No behavior change.
- The deferred React anti-patterns are pre-existing and unchanged — they
  remain lint errors but don't block (CI doesn't gate on lint).
