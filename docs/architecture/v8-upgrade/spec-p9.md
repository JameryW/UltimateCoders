# Spec P9 — Decision Record dashboard trail (read surface)

Blocked by: D19 (decided). Reads the P0/P7 Decision Record; no backend change.

## Goal

Make the attempt-scoped Decision Record readable in the dashboard: verdict, route,
context pointer and local effects per dispatch attempt, on the task detail view.

## What to build

1. **`dashboard/src/lib/decisions.ts`** — types (`DecisionRecord`, `WorkspaceEvidence`,
   `DecisionPagePayload`) and pure formatters:
   - `decisionOrderKey` / `parseDecisionKey` (last-two-colons split, numeric attempt);
   - `decisionTrailQuery(taskId, limit=100)` (encoded `task_id`, bounded window);
   - `decisionRecordedAt` (UTC, degrades without inventing a time);
   - `decisionHeadline` (`succeeded` / `failed (<error_class>)` / `no verdict`);
   - `decisionRouteLine`, `decisionDigestLabel` (12 hex chars);
   - `workspaceLine` (count + up to 3 paths + `+N` + tools + `truncated`);
   - `decisionsFromPayload`, `decisionCountLine` (`N of M attempts`).
2. **`dashboard/src/components/panels/DecisionRecordPanel.tsx`** — a pure
   `DecisionRecordList` and a `DecisionRecordTrail` that fetches the bounded window,
   keeps no stale rows across a task switch, and says "unavailable" instead of
   pretending.
3. **Wiring** in `TaskDetail.tsx` next to the context-compile trail.
4. **Tests** in `dashboard/tests/decisions.test.mjs`: helper behaviour plus SSR render
   of a populated row (verdict, route, digest, effects) and the empty state.

## Acceptance criteria

- [ ] A row shows the verdict, route, `ctx <12 hex>` and the local-effects line.
- [ ] The effects line caps paths with `+N` and says `truncated` when the record was clipped.
- [ ] An empty trail says nothing was recorded; a failed fetch says unavailable.
- [ ] `N of M attempts` is shown when `total` exceeds the window; no re-sorting.
- [ ] No prompt content / diff / tool output can reach the DOM.
- [ ] `tsc -p tsconfig.app.json` and `tsc -p tsconfig.node.json` pass; `vite build` passes.

## Out of scope

- New API endpoints, query parameters or cursor paging
- Subtask-scope selector
- Any write/mutation surface
