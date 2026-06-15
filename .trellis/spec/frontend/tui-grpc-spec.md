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
    subtasks: Map<string, SubtaskItem>;
    events: TaskEvent[];
    isConnected: boolean;
}

function useTaskEvents(client: TaskServiceClient | null, taskId?: string): UseTaskEventsReturn;
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
}
```

**Key behavior**:
- Cursor tracked as grapheme index internally; converted to display column via `stringWidth(textBeforeCursor)` for real cursor positioning
- `onCursorMove` fires on every cursor change (input, backspace, delete, arrow keys, Ctrl+A/E, external value reset)
- Uses `useInput` from Ink for keyboard handling; `cursorRef` (ref) avoids stale closures alongside `cursorGI` (state) for re-render

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

**Decision**: Use `Map<string, SubtaskItem>` keyed by subtask ID. When a new event arrives for an existing subtask, only that entry is updated. This avoids re-creating the entire array on every event and makes lookups O(1).

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
