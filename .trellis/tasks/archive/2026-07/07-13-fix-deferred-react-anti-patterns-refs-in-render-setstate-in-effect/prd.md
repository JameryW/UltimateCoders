# Fix Deferred React Anti-Patterns (refs-in-render + setState-in-effect)

## Background

PR #254 deferred 11 React anti-pattern eslint errors. Research
(`research/react-anti-pattern-fixes.md`) evaluated each: 3 safe to fix,
8 need eslint-disable with documented intent (stable-callback + ref-mirror
pattern — moving to useEffect creates a one-frame race).

## Decisions (locked, per research)

- **D1 (safe fix — dead ref)**: `useDashboardGrpc.ts:288` —
  `connectionStateRef` is a dead write (never read). Delete the ref + its
  assignment. Risk: NONE.
- **D2 (eslint-disable — setState-in-effect, 3 sites)**:
  - `App.tsx:221` — `setGrpcHealthComponents([])` conditional early-return
    reset. Add `// eslint-disable-next-line react-hooks/set-state-in-effect`
    with comment: "conditional reset on disconnect — no cascading render".
  - `TaskDetail.tsx:63` — `setSvg(null); setRenderFailed(false)` conditional
    reset when no deps. Same eslint-disable + comment.
  - `useGrpcWeb.ts:377` — `connect()` in mount-effect is canonical "connect
    on mount". eslint-disable + comment: "connect on mount — canonical pattern".
- **D3 (eslint-disable — refs-in-render, 7 sites)**:
  `useDashboardGrpc.ts:293,391,479` + `useGrpcWeb.ts:156,163,252`. These use
  the **stable-callback + ref-mirror pattern** — `ref.current = value` in
  render is intentional so event handlers / reconnect timers read the latest
  value without re-triggering. Moving to useEffect creates a one-frame race
  (stale ref during reconnect). Add `// eslint-disable-next-line
  react-hooks/refs-in-render` with comment: "stable-callback ref-mirror —
  synchronous to avoid one-frame race in reconnect timers".
- **D4 (TasksPanel.tsx:99)**: borderline setState-in-effect. If
  `highlightTaskId` is set from a same-component event handler, restructure
  to call setState in the handler. If from external (prop/parent), eslint-disable.
  Read context to decide — default to eslint-disable if unclear.
- **Out of scope**: architectural refactor of the ref-mirror pattern (would
  require useCallback/useMemo restructure across hooks — separate task).

## Acceptance Criteria

- [ ] `npm run lint` error count drops from 11 to 0 (all fixed or disabled).
- [ ] `npx tsc --noEmit` green.
- [ ] Dead `connectionStateRef` in useDashboardGrpc.ts deleted.
- [ ] Each eslint-disable has a comment explaining WHY (intent).

## Technical Approach

1. `useDashboardGrpc.ts`: delete `connectionStateRef` (line ~287 declaration
   + ~288 assignment). Verify no reads (research confirmed dead).
2. For each of the 10 remaining: add `// eslint-disable-next-line <rule>`
   with a one-line intent comment.
3. Verify: lint 0 errors, tsc green.
4. No behavior change (eslint-disable + 1 dead-code deletion).

## Risk

- **Dead ref deletion (D1)**: NONE — verified never read.
- **eslint-disable (D2-D4)**: documents existing intentional patterns. No
  behavior change. The ref-mirror pattern is a legitimate React escape hatch.
