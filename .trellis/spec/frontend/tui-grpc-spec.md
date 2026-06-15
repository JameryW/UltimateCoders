# TUI gRPC Client & Hooks Spec

> Executable contracts for the Ink/React TUI gRPC integration — client setup, React hooks, and data flow.

---

## 1. Scope / Trigger

- Trigger: TUI PR2 — Node.js gRPC client connecting to uc-grpc-server
- Cross-layer: Rust gRPC server → Node.js client → React hooks → Ink components
- Requires code-spec depth because it defines streaming behavior, React state management, and offline fallback

---

## 2. Signatures

### TaskServiceClient (`tui/src/grpc/client.ts`)

```typescript
class TaskServiceClient {
    constructor(serverAddress?: string);
    submitTask(request: SubmitTaskRequest): Promise<SubmitTaskResponse>;
    getTask(request: GetTaskRequest): Promise<GetTaskResponse>;
    listTasks(request: ListTasksRequest): Promise<ListTasksResponse>;
    watchTask(request: WatchTaskRequest): ClientReadableStream<TaskEvent>;
    pauseTask(request: PauseTaskRequest): Promise<PauseTaskResponse>;
    resumeTask(request: ResumeTaskRequest): Promise<ResumeTaskResponse>;
    close(): void;
}
```

### useGrpcClient Hook (`tui/src/hooks/useGrpcClient.ts`)

```typescript
interface UseGrpcClientReturn {
    client: TaskServiceClient | null;
    isConnected: boolean;
    isConnecting: boolean;
    error: string | null;
    reconnect: () => void;
}

function useGrpcClient(serverAddress?: string): UseGrpcClientReturn;
```

### useTaskEvents Hook (`tui/src/hooks/useTaskEvents.ts`)

```typescript
interface UseTaskEventsReturn {
    task: TaskProto | null;
    subtasks: SubtaskItem[];        // Array derived from internal Map
    events: TaskEventProto[];       // Capped at MAX_EVENTS (2000)
    isStreaming: boolean;
    setSubtasksFromSubmit: (subtasks: SubtaskProto[], task?: TaskProto) => void;
    updateSubtaskStatus: (subtaskId: string, status: SubtaskStatusType) => void;
    clearTask: () => void;
}

function useTaskEvents(client: TaskServiceClient | null, connectionState: string): UseTaskEventsReturn;
```

### CjkTextInput Component (`tui/src/components/CjkTextInput.tsx`)

```typescript
interface CjkTextInputProps {
    readonly value: string;
    readonly onChange: (value: string) => void;
    readonly onSubmit?: (value: string) => void;
    readonly placeholder?: string;
    readonly focus?: boolean;
    readonly showCursor?: boolean;
    readonly onCursorMove?: (displayCol: number) => void;
    readonly onHistoryNav?: (direction: 'up' | 'down') => void;
}
```

**Key behavior**:
- Cursor tracked as grapheme index internally; converted to display column via `stringWidth(textBeforeCursor)` for real cursor positioning
- `onCursorMove` fires on every cursor change (input, backspace, delete, arrow keys, Ctrl+A/E, external value reset)
- Uses `useInput` from Ink for keyboard handling; `cursorRef` (ref) avoids stale closures alongside `cursorGI` (state) for re-render
- **Ctrl+J**: inserts newline (multi-line task editing)
- **Ctrl+U**: clears entire input
- **Ctrl+K**: deletes from cursor to end of line
- **Up/Down**: delegates to `onHistoryNav` callback (for input history browsing)
- **Pass-through**: Ctrl+C/R/P/Q/F are ignored here and handled by App's global `useInput`

### TUI Reducer (`tui/src/reducer.ts`)

```typescript
interface TuiState {
    messages: ChatMessage[];
    subtasks: SubtaskItem[];
    progress: {completed: number; total: number};
    activeTaskId: string | null;
    followLog: boolean;
    selectedPane: SelectedPane;    // 'input' | 'chat' | 'subtask'
    scrollDirection: 'up' | 'down' | null;
    scrollLines: number;
    scrollTick: number;            // Monotonically increasing — ChatLog detects new scroll commands
    inputHistory: string[];
    historyIndex: number;
    lastError: string | null;
    offlineTimerIds: ReturnType<typeof setTimeout>[];
    eventFilter: EventFilter;      // 'all' | 'task' | 'subtask' | 'tool' | 'error'
    symbolMode: SymbolMode;        // 'unicode' | 'ascii' | 'auto'
}
```

**Key architecture**: Scroll offset is NOT stored in reducer — ChatLog manages `localOffset` internally because the offset must be relative to the **filtered** message list, which the reducer cannot compute. Instead, the reducer tracks `followLog`, `scrollTick`, `scrollDirection`, and `scrollLines`. ChatLog reads `scrollTick` and applies the scroll to its own local offset.

---

## 3. Contracts

### Proto Resolution Order

1. `GRPC_PROTO_PATH` environment variable (if set)
2. Relative path from package: `../../crates/uc-grpc/proto/engine.proto`
3. Hard failure if proto not found

### Connection Lifecycle

| Phase | `isConnecting` | `isConnected` | `error` |
|-------|---------------|---------------|---------|
| Initial | true | false | null |
| Connected | false | true | null |
| Failed | false | false | "Connection refused..." |
| Reconnecting | true | false | null |
| Closed (unmount) | false | false | null |

### WatchTask Stream Events

| Stream Event | Action |
|-------------|--------|
| `data` | Parse TaskEvent, update subtask status map, append to events list |
| `error` | Set `isConnected=false`, attempt reconnect after delay |
| `end` | Set `isConnected=false`, attempt reconnect after delay |

### Subtask Status Mapping (proto string → TUI enum)

| Proto String | SubtaskStatusType |
|-------------|-------------------|
| `"Pending"` | `'pending'` |
| `"Assigned"` | `'assigned'` |
| `"InProgress"` | `'in_progress'` |
| `"Completed"` | `'completed'` |
| `"Failed"` | `'failed'` |
| `"Conflicted"` | `'conflicted'` |
| Unknown | `'pending'` (safe fallback) |

---

## 4. Validation & Error Matrix

### Client Connection Errors

| Condition | Behavior |
|-----------|----------|
| Server not running | `isConnected=false`, `error="Connection refused"`, reconnect on interval |
| Proto file not found | Constructor throws Error with descriptive message |
| Server disconnects mid-stream | `error` event on stream, hook sets `isConnected=false` |
| Invalid server address | Connection fails, same as "server not running" |

### React Hook Guards

| Condition | Behavior |
|-----------|----------|
| `client` is null | `useTaskEvents` returns empty subtasks/events, `isConnected=false` |
| Component unmounts | Stream cancelled, client closed, timers cleared |
| `taskId` changes | Old stream cancelled, new stream started |
| Duplicate events | Ignored (idempotent by event offset) |

---

## 5. Good/Base/Bad Cases

### Connection

- **Good**: Server running → client connects in ~100ms, `isConnected=true`
- **Base**: Server starts after client → client retries, connects when available
- **Bad**: Wrong address → `isConnected=false`, `error` set, user can Ctrl+R to retry

### WatchTask Stream

- **Good**: Stream receives events → subtask map updates in real-time
- **Base**: No events yet → empty subtask map, waiting
- **Bad**: Stream errors → reconnect, subtask map retains last known state

### Offline Fallback (App.tsx)

- **Good**: gRPC connected → SubmitTask goes through server, real decomposition
- **Base**: gRPC disconnected → offline mode, simulated decomposition with local IDs
- **Bad**: gRPC fails mid-submit → error message in ChatLog, user can retry

---

## 6. Tests Required

### Integration / Hook Tests (require React rendering)

| Test | Assertion |
|------|-----------|
| Client constructor | Creates gRPC client with correct address and credentials |
| Client submitTask | Sends SubmitTaskRequest, receives SubmitTaskResponse |
| Client watchTask | Returns ClientReadableStream that emits TaskEvent objects |
| useGrpcClient connect | Sets `isConnected=true` when server available |
| useGrpcClient reconnect | Reconnects when `reconnect()` called |
| useGrpcClient cleanup | Closes client on unmount |
| useTaskEvents stream | Updates subtask map when stream emits events |
| useTaskEvents null client | Returns empty data when client is null |
| useTaskEvents cleanup | Cancels stream on unmount |

### Pure-Function Unit Tests (implemented, 57 tests passing)

All pure functions are tested with vitest. No React rendering needed.

| Module | Tests | Key Assertions |
|--------|-------|----------------|
| `reducer.ts` | 20 | All action types: ADD_MESSAGES (2000 cap), SET_SUBTASKS (progress derivation), UPDATE_SUBTASK_STATUS (unknown id no-op), SCROLL_UP/DOWN (tick increment, followLog toggle), ADD_INPUT_HISTORY (dedup, 50 cap), SET_EVENT_FILTER, CLEAR_TASK/LOG, ADD/CLEAR_OFFLINE_TIMERS, SET_ACTIVE_TASK, SET_FOLLOW_LOG, SET_SELECTED_PANE |
| `formatters.ts` | 13 | All event types (task_submitted/failed/completed, subtask_assigned/started/completed/failed, tool_call/tool_result), unknown type, eventType preservation on all messages, batch conversion, null filtering |
| `symbols.ts` | 9 | unicode mode, ascii mode, auto mode env detection (CI, NO_COLOR, TERM=dumb → ascii; TERM=xterm-256color → unicode), forced override, auto→CI resolution |
| `truncate.ts` | 8 | Short text (no truncation), exact width, long English + ellipsis, CJK 2-column chars, combining chars not split, ZWJ emoji not split, empty string, width=1 |
| `filter.ts` | 7 | all/task/subtask/tool/error filters, user messages always pass, messages without eventType pass all filters |

**Test framework**: vitest (configured in `tui/vitest.config.ts`)
**Run command**: `cd tui && npx vitest run`
**Typecheck**: `cd tui && npm run typecheck`

---

## 7. Wrong vs Correct

### Wrong: Not cancelling WatchTask stream on unmount

```typescript
// Wrong — stream keeps running after component unmounts
useEffect(() => {
    const stream = client.watchTask({});
    stream.on('data', (event) => { /* update state */ });
    // Missing: return cleanup function
}, [client]);
```

#### Correct

```typescript
// Correct — cancel stream on unmount or client change
useEffect(() => {
    const stream = client.watchTask({});
    stream.on('data', (event) => { /* update state */ });
    return () => { stream.cancel(); };
}, [client]);
```

### Wrong: Generating offline IDs with Date.now() in a loop

```typescript
// Wrong — same millisecond = duplicate IDs
subtasks.map((desc, i) => ({
    id: `st-${Date.now()}`,  // All get same timestamp!
}));
```

#### Correct

```typescript
// Correct — unique base ID + per-item suffix
const baseId = Date.now().toString(36) + Math.random().toString(36).slice(2);
subtasks.map((desc, i) => ({
    id: `${baseId}-${i}`,
}));
```

---

## Design Decisions

### Decision: Offline fallback mode

**Context**: The TUI should work even when the gRPC server is not running (e.g., during development or when running standalone).

**Decision**: When gRPC is unavailable, `App.tsx` falls back to offline mode that simulates task decomposition with locally-generated IDs and static subtask statuses. This ensures the TUI is always interactive. The StatusBar shows connection status so users know when they're in offline mode.

### Decision: CjkTextInput replacing ink-text-input

**Context**: `ink-text-input` uses `input.length` (JS code units) for cursor positioning, which breaks for CJK characters that occupy 2 terminal columns but have `length` 1. This causes misaligned terminal cursor, broken IME composition windows, incorrect arrow/backspace behavior, and garbled rendering.

**Options Considered**:
1. Patch ink-text-input with string-width — Fragile; internal `.length`/`slice` calls are scattered and not pluggable
2. Replace with CjkTextInput — Full control over width calculation and cursor movement

**Decision**: Use custom `CjkTextInput` component that:
- Uses `string-width` (v8) for all display-width calculations
- Uses `grapheme-splitter` (v1) for grapheme-cluster-based cursor movement
- Deletes whole grapheme clusters on backspace/delete (not single code units)
- Positions the real terminal cursor via `onCursorMove` callback with display-width column

**Example**:
```typescript
// WRONG: JS length ≠ terminal display width
setCursorPosition({x: 4 + value.length, y: 0});  // "中文" → 4+2=6 (should be 4+4=8)

// CORRECT: string-width returns terminal column count
import stringWidth from 'string-width';
setCursorPosition({x: 5 + stringWidth(value), y: 0});  // "中文" → 5+4=9
```

**Extensibility**: The `onCursorMove(displayCol: number)` callback pattern decouples cursor positioning from the input component, allowing any parent to position the real terminal cursor correctly regardless of layout changes.

### Decision: Subtask state as Map<string, SubtaskItem>

**Context**: `useTaskEvents` needs to maintain subtask state that updates incrementally as events arrive.

**Decision**: Use `Map<string, SubtaskItem>` keyed by subtask ID internally, exposed as `SubtaskItem[]` array. When a new event arrives for an existing subtask, only that entry is updated. This avoids re-creating the entire array on every event and makes lookups O(1). The `processEvent()` function returns a new Map (immutable update pattern) rather than mutating in place.

### Decision: useReducer over multiple useState

**Context**: App.tsx had 6+ useState + 2 render-side-effect synchronizations, causing stale state and inconsistent subtask updates.

**Options Considered**:
1. Multiple useState with useEffect sync — Fragile; useEffect chains create cascading re-renders
2. Custom hook `useTuiModel` wrapping useState — Incremental but still has sync issues
3. useReducer with single TuiState — Single dispatch path, predictable state transitions

**Decision**: useReducer with a single `TuiState` object and 15+ action types. All state transitions go through `dispatch(action)`. The render path contains zero setState calls. This makes state changes traceable and prevents the "setState during render" anti-pattern that caused subtask sync bugs.

**Example**:
```typescript
// WRONG: setState in render path causes infinite re-render risk
const prevLen = useRef(0);
if (streamSubtasks.length !== prevLen.current) {
  prevLen.current = streamSubtasks.length;
  setSubtasks(streamSubtasks);  // setState during render!
}

// CORRECT: useEffect for side effects, dispatch for state changes
useEffect(() => {
  if (streamSubtasks.length === 0) return;
  const changed = streamSubtasks.some((st, i) =>
    !prev[i] || prev[i].status !== st.status
  );
  if (changed) dispatch({type: 'SET_SUBTASKS', subtasks: streamSubtasks});
}, [streamSubtasks]);
```

### Decision: Scroll offset in ChatLog component, not reducer

**Context**: ChatLog supports event filtering (all/task/subtask/tool/error). The scroll offset must be relative to the **filtered** message list, not the full list. The reducer cannot compute the filtered list.

**Options Considered**:
1. Store `logOffset` in reducer — Wrong: offset indexes into full list but is applied to filtered list
2. Store `logOffset` in reducer + derive filtered offset — Fragile: requires reducer to know filter logic
3. Store scroll commands in reducer, offset in ChatLog — Correct: reducer emits intent, component applies to its local view

**Decision**: The reducer tracks `followLog`, `scrollTick`, `scrollDirection`, `scrollLines`. ChatLog maintains `localOffset` (useState) relative to its filtered message list. When `scrollTick` changes, ChatLog applies the scroll direction/lines to `localOffset`. When `followLog` is true, `localOffset` is pinned to the bottom.

**Example**:
```typescript
// In ChatLog:
const [localOffset, setLocalOffset] = useState(0);
const prevTick = useRef(state.scrollTick);

useEffect(() => {
  if (state.scrollTick === prevTick.current) return;
  prevTick.current = state.scrollTick;
  if (state.scrollDirection === 'up') {
    setLocalOffset(prev => Math.max(0, prev - state.scrollLines));
  } else if (state.scrollDirection === 'down') {
    setLocalOffset(prev => Math.min(maxOffset, prev + state.scrollLines));
  }
}, [state.scrollTick]);
```

### Decision: Symbol strategy (unicode/ascii/auto)

**Context**: Different terminals have different Unicode support. CI environments, dumb terminals, and NO_COLOR settings often can't render Unicode box-drawing characters or special symbols.

**Decision**: `tui/src/symbols.ts` provides three modes: `unicode` (full symbols), `ascii` (safe fallback like `[x]`, `[ ]`), `auto` (detects from environment). The `auto` mode checks:
- `CI` env var → ASCII
- `NO_COLOR` env var → ASCII
- `TERM=dumb` → ASCII
- `TERM` contains xterm/screen/tmux → Unicode
- `TERM_PROGRAM` contains iTerm/WezTerm/kitty → Unicode
- `LC_ALL`/`LANG` contains UTF-8 → Unicode
- Default → ASCII (conservative)

The symbol set is passed from App → SubtaskTree as a `SymbolSet` prop rather than imported directly, keeping components testable and the strategy centralized.

---

## Common Mistakes

1. **Not cancelling gRPC streams on component unmount** — Always return a cleanup function from `useEffect` that calls `stream.cancel()`. Without this, stale streams leak and may try to update unmounted state.

2. **Using `useRef` for values that downstream hooks depend on** — If `useGrpcClient` stores the client in a `useRef`, changing it won't trigger re-renders in components that depend on `client`. Use `useState` for the reactive value and `useRef` only for cleanup.

3. **Accessing proto fields with snake_case** — `@grpc/proto-loader` defaults to `keepCase: false`, converting `task_id` to `taskId`. Always use camelCase in JavaScript.

4. **Not handling stream `end` event** — When the server closes the stream, the `end` event fires, not `error`. Both must be handled to set `isConnected=false`.

5. **Hardcoding proto path** — Always resolve relative to the package directory or use `GRPC_PROTO_PATH`. Absolute paths break portability.

6. **Using JS `.length` for terminal column positions** — `string.length` counts UTF-16 code units, not terminal display columns. CJK characters occupy 2 columns but have `length` 1. Always use `string-width` for column math. This applies to cursor positioning, padding calculations, and any ANSI escape column arguments.

7. **Using JS string indexing (`[0]`, `slice(1)`) on multi-code-point graphemes** — Emoji with ZWJ sequences, combining characters, and CJK surrogate pairs span multiple code units. Use `GraphemeSplitter.splitGraphemes()` then index the resulting array. Applies especially to placeholder rendering where `placeholder[0]` may slice a grapheme in half.

8. **Calculating cursor x-offset without accounting for Box layout** — Ink's `<Box marginX paddingX borderStyle>` each consume terminal columns. The total offset for TaskInput's cursor is: `marginX(1) + left-border(1) + paddingX(1) + "> "(2) = 5` columns, not 4. Recalculate if the layout changes.

9. **Storing scroll offset in reducer when the view is filtered** — When ChatLog has an event filter active, the visible messages are a subset of the full list. An offset that indexes into the full list will point to the wrong message when applied to the filtered list. Always manage scroll offset locally in the component that knows the filtered list.

10. **Using `string.slice(0, -1)` for terminal-width truncation** — This can split a grapheme cluster (combining characters, ZWJ emoji, CJK with variant selectors), producing malformed strings. Use `GraphemeSplitter.splitGraphemes()` and remove from the end by grapheme, tracking cumulative width with `stringWidth()`.

11. **Not resetting `processedEventCount` when events are cleared** — `useTaskEvents` appends to an internal `events` array. If `clearTask()` empties this array but the consumer's `processedEventCount` ref retains the old count, new events are silently skipped until the count exceeds the old threshold. Always reset the counter when the source array is cleared.

12. **Not capping event/message arrays** — gRPC streams can emit events indefinitely. Both `useTaskEvents` and the reducer's `ADD_MESSAGES` must cap at ~2000 entries. Without this, long-running sessions consume unbounded memory.

13. **Multiple `useInput` hooks with overlapping key bindings** — Ink 5 allows multiple `useInput` hooks, but they all fire for every keypress. If `CjkTextInput` and `App` both handle Ctrl+R, the action fires twice. Solution: CjkTextInput explicitly ignores Ctrl+C/R/P/Q/F/L (passes them through to App's global handler). The `focus` prop on `useInput({isActive})` controls which component processes printable input.

---

## TUI Testing Conventions

### Testable vs Untestable Boundaries

| Testable (pure functions) | Untestable without React rendering |
|---------------------------|-----------------------------------|
| `reducer.ts` — state transitions | Ink component rendering |
| `formatters.ts` — event formatting | `useInput` keyboard handling |
| `symbols.ts` — symbol resolution | gRPC client (needs proto mock) |
| `truncate.ts` — CJK-safe truncation | `useGrpcClient` / `useTaskEvents` hooks |
| `filter.ts` — event filter logic | `CjkTextInput` cursor behavior |

### Reducer Test Pattern

Test each action type independently with a minimal initial state:

```typescript
it('caps at 2000 messages', () => {
  const msgs = Array.from({length: 2001}, (_, i) => makeMsg(`m${i}`));
  const result = reducer(
    {...initialState, messages: msgs.slice(0, 2000)},
    {type: 'ADD_MESSAGES', messages: [makeMsg('overflow')]}
  );
  expect(result.messages).toHaveLength(2000);
  expect(result.messages[1999].text).toBe('overflow'); // newest kept
});
```

Key patterns:
- Use a `makeMsg(text, overrides?)` factory for test messages
- Test boundary values (0, 2000 cap, 50 history cap)
- Test no-op cases (unknown subtask id, empty state)

### Truncate Test Pattern

CJK truncation must test grapheme boundaries, not byte/code-unit boundaries:

```typescript
it('does not split ZWJ emoji', () => {
  // 👨‍👩‍👧 = man + ZWJ + woman + ZWJ + girl (single grapheme, ~2 columns)
  const result = truncateToWidth('👨‍👩‍👧abc', 5);
  // Should not split the ZWJ sequence — either keep whole emoji or drop it
  expect(result === '👨‍👩‍👧…' || result === '…').toBeTruthy();
});
```

Key patterns:
- Test combining characters (é = e + combining accent)
- Test ZWJ emoji (👨‍👩‍👧)
- Test CJK width: each CJK char = 2 terminal columns
- Test edge cases: empty string, width=1

### Symbols Test Pattern

Mock `process.env` for auto-mode detection:

```typescript
it('auto mode falls back to ascii in CI', () => {
  const origCI = process.env.CI;
  process.env.CI = 'true';
  const syms = getSymbols('auto');
  expect(syms.check).toBe('[x]');
  process.env.CI = origCI;
});
```

Key patterns:
- Save and restore `process.env` values
- Test each env variable independently (CI, NO_COLOR, TERM, TERM_PROGRAM)
- Test forced modes (unicode/ascii) without env manipulation
