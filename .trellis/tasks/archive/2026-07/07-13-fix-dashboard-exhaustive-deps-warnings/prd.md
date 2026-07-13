# Fix Dashboard exhaustive-deps Warnings

## Background

PR #254/#255 fixed all 28 dashboard eslint **errors** (now 0). 19
**warnings** remain — all `react-hooks/exhaustive-deps`. 12 are the same
`getTransport` missing-dependency across useCallback hooks in
`useGrpcWeb.ts`. `getTransport` is `useCallback(() => getSharedTransport(), [])`
— stable (empty deps), so adding it to dependent callbacks' deps is safe
(won't trigger re-create). The other 7 are various useEffect/useMemo with
missing deps.

## Decisions (locked)

- **D1**: Add `getTransport` to the 12 useCallback deps arrays in
  `useGrpcWeb.ts`. Safe — `getTransport` is stable (empty-deps useCallback),
  so adding it doesn't change behavior.
- **D2**: For the 7 other warnings (useEffect missing `dashboard`/`selectedRepo`/
  `getSchedulerStatus`/`listEvents`/`listTasks`/`listWorkers`; useMemo missing
  `TIME_RANGE_MS`; subtasks logical expression): read each context. If the dep
  is stable (useCallback/useMemo/useState setter), add it. If adding would
  cause unwanted re-trigger (e.g. `dashboard` object changes every render),
  use `// eslint-disable-next-line react-hooks/exhaustive-deps` with a comment
  explaining why.
- **D3**: `TIME_RANGE_MS` — if it's a module constant, add to deps (safe).
- **Out of scope**: architectural refactor of `dashboard` object stability
  (would require useMemo on the whole hook return — separate task).

## Acceptance Criteria

- [ ] `npm run lint` warning count drops from 19 to ≤5.
- [ ] `npx tsc --noEmit` green.
- [ ] No behavior change (deps additions for stable refs only; eslint-disable
      for intentionally-omitted unstable deps with comment).

## Technical Approach

1. Run lint, capture all 19 warnings with file:line.
2. `useGrpcWeb.ts`: add `getTransport` to each useCallback deps array (12 sites).
3. Other 7: read context, add stable deps or eslint-disable with comment.
4. Verify: lint warnings ≤5, tsc green.

## Risk

- **Low**: adding stable deps (getTransport, TIME_RANGE_MS) is safe.
- **Medium**: `dashboard` object deps — if it changes every render, adding
  causes re-trigger. Use eslint-disable for those.
