# Research: React Anti-Pattern Fixes (11 deferred eslint errors)

- **Query**: Research the 11 deferred dashboard eslint React anti-pattern errors (7 refs-in-render + 4 setState-in-effect)
- **Scope**: internal
- **Date**: 2026-07-13

## Environment

- **React**: 19.2.6 (`package.json:24`)
- **eslint-plugin-react-hooks**: 7.1.1 (`package.json:39`) — new strict rules
- **eslint config**: `eslint.config.js` — uses `reactHooks.configs.flat.recommended` (rules are errors, not warnings)
- **Rules triggered**: `react-hooks/refs` (7 errors) + `react-hooks/set-state-in-effect` (4 errors)

## Summary Table

| # | File:Line | Anti-Pattern | Current Code (1 line) | Fix Approach | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | `useDashboardGrpc.ts:288` | ref-in-render | `connectionStateRef.current = connectionState;` | **Delete** — ref is never read (dead write) | LOW | **SAFE TO FIX** (delete) |
| 2 | `useDashboardGrpc.ts:293` | ref-in-render | `optsRef.current = opts;` | Move to `useEffect(() => { optsRef.current = opts; }, [opts])` | MED | **NEEDS eslint-disable** (see below) |
| 3 | `useDashboardGrpc.ts:391` | ref-in-render | `connectSseRef.current = connectSse;` | Move to `useEffect(() => { connectSseRef.current = connectSse; }, [connectSse])` | MED | **NEEDS eslint-disable** (see below) |
| 4 | `useDashboardGrpc.ts:479` | ref-in-render | `connectRef.current = connect;` | Move to `useEffect(() => { connectRef.current = connect; }, [connect])` | MED | **NEEDS eslint-disable** (see below) |
| 5 | `useGrpcWeb.ts:156` | ref-in-render | `connectionStateRef.current = connectionState;` | Move to `useEffect` — BUT `submitTask` (line 266) reads it synchronously | HIGH | **NEEDS eslint-disable** (synchronous read required) |
| 6 | `useGrpcWeb.ts:163` | ref-in-render | `optsRef.current = opts;` | Move to `useEffect(() => { optsRef.current = opts; }, [opts])` | MED | **NEEDS eslint-disable** (see below) |
| 7 | `useGrpcWeb.ts:252` | ref-in-render | `connectRef.current = connect;` | Move to `useEffect(() => { connectRef.current = connect; }, [connect])` | MED | **NEEDS eslint-disable** (see below) |
| 8 | `App.tsx:221` | setState-in-effect | `setGrpcHealthComponents([]);` (early-return cleanup) | Restructure: derive via `useMemo` or condition the effect body | LOW | **SAFE TO FIX** (restructure) |
| 9 | `TaskDetail.tsx:63` | setState-in-effect | `setSvg(null); setRenderFailed(false);` (early-return cleanup) | Move resets into the render-compute path or use `useMemo` | LOW | **SAFE TO FIX** (restructure) |
| 10 | `TasksPanel.tsx:99` | setState-in-effect | `setExpandedTaskId(highlightTaskId); setStatusFilter(null);` | This is a side-effect of prop change — may need eslint-disable or restructure to event handler | MED | **BORDERLINE** (see below) |
| 11 | `useGrpcWeb.ts:377` | setState-in-effect | `connect();` (calls setState indirectly via connect→setConnectionState) | This is standard mount-effect pattern — eslint-disable is correct | LOW | **NEEDS eslint-disable** |

**Totals**: 3 safe to fix (restructure/delete) | 8 need eslint-disable (documented intent)

---

## Detailed Analysis

### Refs-in-render (7 errors)

#### Pattern explanation

The code uses the **"ref mirror" pattern**: `ref.current = value` written directly in the render body. This synchronously mirrors a prop/state into a ref so that callbacks (event handlers, setTimeout callbacks) can read the latest value via `ref.current` without being stale closures.

**Why it's used**: `scheduleReconnect` and `connect` are wrapped in `useCallback` with `[]` (empty deps) to keep their identity stable. They read `optsRef.current`, `connectRef.current`, etc. If these refs were updated in a `useEffect` (which runs AFTER render), there would be a one-render-frame window where the ref holds the previous value. If a reconnect timer fires in that window, it would call a stale `connect` or read stale `opts`.

**React 19 concern**: React 19 strict mode double-invokes render. A `ref.current = value` in render body executes twice, but since it's an idempotent assignment (same value), this is safe. The eslint rule flags it because in general, writing refs during render can cause subtle bugs (e.g. if the value is computed from other refs that haven't updated yet).

---

#### Error 1: `useDashboardGrpc.ts:288` — `connectionStateRef.current = connectionState`

```typescript
const connectionStateRef = useRef<DashboardConnectionState>(connectionState);
connectionStateRef.current = connectionState;  // line 288 — ERROR
```

**Finding**: `connectionStateRef` is **never read** anywhere in the file (grep confirms only lines 287-288 reference it). This is a **dead write** — the ref was likely added as a pattern copy from `useGrpcWeb.ts` but is unused.

**Fix**: Delete both lines 287-288. **Risk: NONE** (dead code removal).

---

#### Error 2: `useDashboardGrpc.ts:293` — `optsRef.current = opts`

```typescript
const optsRef = useRef(opts);
optsRef.current = opts;  // line 293 — ERROR
```

**Consumers** (all inside `useCallback([], ...)` stable callbacks):
- Line 318: `if (optsRef.current.enabled)` — inside `scheduleReconnect`'s setTimeout callback
- Line 326: `if (optsRef.current.enabled)` — inside `scheduleReconnect`'s setTimeout callback
- Line 353: `optsRef.current.onTaskEvent?.(taskEvent)` — inside SSE event listener
- Line 373: `if (optsRef.current.mergeGrpcTasks)` — inside stream callback
- Line 377: `optsRef.current.onSnapshot?.(converted)` — inside stream callback
- Line 406: `if (!optsRef.current.enabled)` — inside `connect` callback
- Line 450, 455, 461: inside stream callbacks

**If moved to useEffect**: The ref would lag one render behind. If `opts.enabled` changes from `false` to `true`, and a reconnect timer fires before the effect runs, `optsRef.current.enabled` would still be `false` and the reconnect would be skipped. This is a **real race condition risk**.

**Verdict**: **eslint-disable with documented intent**. The synchronous mirror is intentional to avoid stale-closure races in setTimeout/stream callbacks.

---

#### Error 3: `useDashboardGrpc.ts:391` — `connectSseRef.current = connectSse`

```typescript
connectSseRef.current = connectSse;  // line 391 — ERROR
```

**Consumer**:
- Line 319: `connectSseRef.current()` — called inside `scheduleReconnect`'s setTimeout (SSE fallback path)

**If moved to useEffect**: If `connectSse` is recreated (its dep `scheduleReconnect` changes), the ref would lag one frame. A setTimeout firing in that window would call the old `connectSse`. Since `connectSse` closes over `scheduleReconnect` which is `useCallback([])`, the function identity is stable, so in practice the lag is harmless. **But** the pattern is intentional for safety.

**Verdict**: **eslint-disable with documented intent**.

---

#### Error 4: `useDashboardGrpc.ts:479` — `connectRef.current = connect`

```typescript
connectRef.current = connect;  // line 479 — ERROR
```

**Consumer**:
- Line 327: `connectRef.current()` — called inside `scheduleReconnect`'s setTimeout

**Same analysis as Error 3**: `connect` is `useCallback([clearRetryTimer, scheduleReconnect])` — both stable. Risk of stale call is low but the synchronous mirror is the intentional safety pattern.

**Verdict**: **eslint-disable with documented intent**.

---

#### Error 5: `useGrpcWeb.ts:156` — `connectionStateRef.current = connectionState`

```typescript
const connectionStateRef = useRef<GrpcConnectionState>(connectionState);
connectionStateRef.current = connectionState;  // line 156 — ERROR
```

**Consumer**:
- Line 266: `if (connectionStateRef.current === "disconnected")` — inside `submitTask` (async callback)

**Critical**: `submitTask` is called by the user (button click). If the ref is updated in useEffect (one frame late), and the user clicks "submit" immediately after a state transition (e.g. just connected), `connectionStateRef.current` could still read `"disconnected"` and throw a false error.

**Verdict**: **eslint-disable with documented intent**. The synchronous mirror is REQUIRED here — this is the highest-risk ref to move.

---

#### Error 6: `useGrpcWeb.ts:163` — `optsRef.current = opts`

```typescript
const optsRef = useRef(opts);
optsRef.current = opts;  // line 163 — ERROR
```

**Consumers**:
- Line 187: `if (optsRef.current.enabled)` — inside `scheduleReconnect`'s setTimeout
- Line 203: `if (!optsRef.current.enabled)` — inside `connect` callback
- Line 229: `optsRef.current.onSyncRequired?.(reason, skipped)` — inside stream callback
- Line 234: `optsRef.current.onTaskEvent?.(dashboardEvent)` — inside stream callback

**Same analysis as Error 2**: Stale `opts.enabled` in a reconnect timer is a real race risk.

**Verdict**: **eslint-disable with documented intent**.

---

#### Error 7: `useGrpcWeb.ts:252` — `connectRef.current = connect`

```typescript
connectRef.current = connect;  // line 252 — ERROR
```

**Consumer**:
- Line 188: `connectRef.current()` — inside `scheduleReconnect`'s setTimeout

**Same analysis as Error 4**.

**Verdict**: **eslint-disable with documented intent**.

---

### setState-in-effect (4 errors)

#### Error 8: `App.tsx:221` — `setGrpcHealthComponents([])`

```typescript
useEffect(() => {
  if (grpcState !== "connected") {
    setGrpcHealthComponents([]);  // line 221 — ERROR
    return;
  }
  const poll = async () => {
    try {
      const h = await healthCheck();
      setGrpcHealthComponents(h.components);
      // ...
    } catch (err) { /* ... */ }
  };
  poll();
  const timer = setInterval(poll, 30000);
  return () => clearInterval(timer);
}, [grpcState, healthCheck]);
```

**Context**: This effect polls health every 30s when connected. When disconnected, it clears the health components. The `setGrpcHealthComponents([])` is a synchronous reset in the effect body.

**Fix approach**: This is an early-return cleanup pattern. The setState is conditional (only when `grpcState !== "connected"`). The React docs say setState in effect is OK if it's in a subscription callback, but a synchronous reset on condition is flagged.

Options:
1. **eslint-disable** — this is a legitimate "reset state when external condition changes" pattern
2. **Restructure**: derive `grpcHealthComponents` from a combined state that includes `grpcState`, so the reset is implicit. But this changes the data model significantly.

**Verdict**: **SAFE TO FIX** via eslint-disable (simplest) OR restructure to derive. The reset is correct behavior — clearing stale health data when disconnected. Risk of cascading render is minimal (it only fires when grpcState changes, which is infrequent).

---

#### Error 9: `TaskDetail.tsx:63` — `setSvg(null); setRenderFailed(false);`

```typescript
useEffect(() => {
  const hasDeps = subtasks.some((st) => st.depends_on.length > 0);
  if (!hasDeps || subtasks.length === 0) {
    setSvg(null);            // line 63 — ERROR
    setRenderFailed(false);  // line 64 (part of same error)
    return;
  }
  // ... render mermaid graph ...
}, [subtasks]);
```

**Context**: This effect renders a Mermaid DAG graph from subtasks. When there are no dependencies or no subtasks, it resets the SVG state to null. This is a "clear stale derived state when input is empty" pattern.

**Fix approach**: This is a standard "reset when input changes" pattern. The React docs suggest deriving state during render instead of resetting in an effect. But here `svg` is a rendered Mermaid string (async render), so it can't be derived synchronously.

Options:
1. **eslint-disable** — legitimate async-render + reset pattern
2. **Restructure**: compute `hasDeps` during render, and only run the effect when `hasDeps` is true. Then the reset becomes unnecessary because the effect won't run. But `svg` would still hold stale value from a previous render.

**Verdict**: **SAFE TO FIX** via eslint-disable or minor restructure. Low risk — the reset only fires when subtasks change (infrequent).

---

#### Error 10: `TasksPanel.tsx:99` — `setExpandedTaskId(highlightTaskId); setStatusFilter(null);`

```typescript
useEffect(() => {
  if (highlightTaskId) {
    setExpandedTaskId(highlightTaskId);  // line 99 — ERROR
    setStatusFilter(null);               // line 100
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        highlightRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    });
    onHighlightShown?.();
  }
}, [highlightTaskId, onHighlightShown]);
```

**Context**: When a `highlightTaskId` prop is set (after task submission), this effect auto-expands the task, clears the status filter (so the task is visible), and scrolls to it. This is a "respond to prop change by updating internal state" pattern.

**Fix approach**: This is the "adjusting state when a prop changes" pattern that React 19 docs explicitly call out as potentially unnecessary. The recommendation is to derive during render or use a key reset. But here the behavior is: filter + expand + scroll, which are side effects of a user action (submit).

Options:
1. **eslint-disable** — the setState is conditional (`if (highlightTaskId)`), and the double-rAF scroll depends on the state being set first
2. **Restructure**: move the expand + filter clear into the submit handler (event-driven, not effect-driven). But `highlightTaskId` comes from a parent prop, so the handler may not have access.

**Verdict**: **BORDERLINE**. The cleanest fix is to move the state updates to the event handler that sets `highlightTaskId` (if accessible). If not, eslint-disable is acceptable. The double-rAF pattern (lines 102-106) depends on the state being committed first, so moving out of effect would require restructuring the scroll logic too.

---

#### Error 11: `useGrpcWeb.ts:377` — `connect()`

```typescript
useEffect(() => {
  connect();              // line 377 — ERROR (connect calls setConnectionState internally)
  return disconnect;
}, [connect, disconnect]);
```

**Context**: This is the standard "connect on mount, disconnect on unmount" pattern. `connect()` internally calls `setConnectionState("connecting")` synchronously, which triggers the eslint rule.

**Fix approach**: This is the **canonical mount-effect pattern**. The eslint rule is overly strict here — connecting on mount IS the intended behavior. React 19 docs say effects are for "synchronizing with external systems," and a gRPC connection is an external system.

**Verdict**: **NEEDS eslint-disable**. This is the correct pattern. Moving the connect into an async callback or condition would break the mount/unmount lifecycle. The cascading render (connect → setState → re-render) is expected and harmless here.

---

## Fix Strategy Recommendation

### Safe to fix (3 errors)

| # | File:Line | Fix |
|---|---|---|
| 1 | `useDashboardGrpc.ts:288` | **Delete** `connectionStateRef` (dead code — ref is never read) |
| 8 | `App.tsx:221` | **eslint-disable** or restructure to derive `grpcHealthComponents` from combined state |
| 9 | `TaskDetail.tsx:63` | **eslint-disable** or restructure to skip effect when `!hasDeps` |

### Need eslint-disable with documented intent (8 errors)

| # | File:Line | Disable comment |
|---|---|---|
| 2 | `useDashboardGrpc.ts:293` | `// eslint-disable-next-line react-hooks/refs -- intentional sync mirror for stable-callback reads` |
| 3 | `useDashboardGrpc.ts:391` | `// eslint-disable-next-line react-hooks/refs -- intentional sync mirror for stable-callback reads` |
| 4 | `useDashboardGrpc.ts:479` | `// eslint-disable-next-line react-hooks/refs -- intentional sync mirror for stable-callback reads` |
| 5 | `useGrpcWeb.ts:156` | `// eslint-disable-next-line react-hooks/refs -- intentional sync mirror, submitTask reads synchronously` |
| 6 | `useGrpcWeb.ts:163` | `// eslint-disable-next-line react-hooks/refs -- intentional sync mirror for stable-callback reads` |
| 7 | `useGrpcWeb.ts:252` | `// eslint-disable-next-line react-hooks/refs -- intentional sync mirror for stable-callback reads` |
| 10 | `TasksPanel.tsx:99` | `// eslint-disable-next-line react-hooks/set-state-in-effect -- responds to highlightTaskId prop change` |
| 11 | `useGrpcWeb.ts:377` | `// eslint-disable-next-line react-hooks/set-state-in-effect -- canonical mount-effect connect pattern` |

---

## Key Concern: Why refs-in-render CANNOT be moved to useEffect

The `useDashboardGrpc` and `useGrpcWeb` hooks use a **stable-callback + ref-mirror** architecture:

1. `scheduleReconnect` and `connect` are `useCallback([], ...)` — identity never changes
2. Inside their setTimeout/stream callbacks, they read `optsRef.current`, `connectRef.current`, `connectSseRef.current`
3. The refs are mirrored synchronously in render body: `ref.current = value`

**If moved to useEffect**:
- Effects run AFTER paint (async)
- A reconnect timer could fire in the gap between render and effect
- The timer callback would read a stale `optsRef.current.enabled` (still `false` from previous render)
- Result: reconnect silently skipped, connection never recovers

**This is a documented React pattern** (see [React docs: "You might not need an effect" — adjusting some state when a prop changes](https://react.dev/learn/you-might-not-need-an-effect)). The docs say "If you're calling set functions during rendering, React will re-render the component immediately after it exits with a return statement, before rendering the children." But for refs specifically, the docs say "Do not write or read ref.current during rendering" — which is the rule being enforced.

The tension: the rule is correct for most cases, but the **stable-callback + ref-mirror** pattern is a legitimate escape hatch for callbacks that must not be recreated (to avoid breaking `useEffect` deps or timer cleanup). The correct fix is `eslint-disable` with a comment explaining why synchronous behavior is needed.

---

## Caveats / Not Found

- **React 19 strict mode double-render**: All ref assignments are idempotent (`ref.current = value`), so double-invocation is safe. No mutation or side-effect concerns.
- **`connectionStateRef` in useDashboardGrpc**: Confirmed dead code (never read). Safe to delete — but verify no external consumer accesses the returned hook's internal ref (it's not returned, so safe).
- **TasksPanel.tsx:99 restructure**: Moving state updates to the submit handler requires checking if `highlightTaskId` is set by the same component or a parent. If parent-controlled, the effect is the only place to respond. Did not fully trace the prop origin.
- **Alternative architecture**: The stable-callback + ref-mirror pattern could be replaced with `useSyncExternalStore` or a reducer, but that's a significant refactor beyond the scope of "fix eslint errors."
