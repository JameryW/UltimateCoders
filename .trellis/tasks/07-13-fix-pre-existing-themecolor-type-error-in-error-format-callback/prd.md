# Fix Pre-existing ThemeColor Type Error in error-format Callback

## Background

`packages/uc-orchestrator/src/ui/error-format.ts` `formatErrorForDisplay`
has a callback param `fgColored: (color: string, text: string) => string`.
Callers pass `(c, t) => theme.fg(c, t)` where `theme.fg` expects
`ThemeColor` (not `string`). This causes a pre-existing TS2345 error at
3 call sites (progress-widget.ts:145, task-result-renderer.ts:61,
subtask-tree-overlay.ts:110). PRs #251/#252 noted it as pre-existing
(shifted from :139 to :145). Fix it to clean OMP tsc.

## What I already know (verified this session)

- `error-format.ts:98` `fgColored: (color: string, text: string) => string` — `color` is `string`.
- `error-format.ts` only calls `fgColored("error", ...)` — always the literal `"error"`.
- `ThemeColor` is exported from `@oh-my-pi/pi-coding-agent` (`node_modules/@oh-my-pi/pi-coding-agent/src/modes/theme/theme.ts:897`). progress-widget.ts already imports `Theme` from the same module.
- 3 call sites pass `(c, t) => theme.fg(c, t)` — `c: string` → `theme.fg(c)` errors (expects ThemeColor).
- `error-format.test.ts` may have mock callbacks with `(color: string, ...)` — need to check + sync.

## Decisions (locked)

- **D1**: `error-format.ts` import `ThemeColor` from `@oh-my-pi/pi-coding-agent`; change `fgColored` param `color: string` → `color: ThemeColor`.
- **D2**: 3 call sites unchanged — `(c, t) => theme.fg(c, t)` now type-checks (c is ThemeColor).
- **D3**: `error-format.test.ts` — if mock callbacks use `(color: string, ...)`, update to `(color: ThemeColor, ...)` or cast the test's color arg. Check + sync.
- **Out of scope**: other pre-existing errors (vendor prompt modules, scheduler dispatchMode).

## Acceptance Criteria

- [ ] `error-format.ts` `fgColored` param is `ThemeColor`.
- [ ] OMP `bun run check` shows 0 errors in progress-widget.ts, task-result-renderer.ts, subtask-tree-overlay.ts, error-format.ts (the 3 call sites + def).
- [ ] `error-format.test.ts` passes (bun test).
- [ ] No new errors introduced.

## Technical Approach

1. `error-format.ts`: add `import type { ThemeColor } from "@oh-my-pi/pi-coding-agent";`; change line 98 `fgColored: (color: string, text: string)` → `fgColored: (color: ThemeColor, text: string)`.
2. Check `error-format.test.ts` for mock callbacks — update if needed.
3. Verify `bun run check` — the 3 TS2345 errors at the call sites should resolve.
4. `bun test` for error-format.test.ts.

## Risk

- **Low**: narrowing `string` → `ThemeColor` is a type-only change. error-format.ts only ever passes `"error"` (a valid ThemeColor). Callers already pass theme.fg which takes ThemeColor. Tests may need a cast if they pass arbitrary strings.
