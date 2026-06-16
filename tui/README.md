# UltimateCoders TUI

Terminal UI for the UltimateCoders distributed AI coding system, built with [Ink](https://github.com/vadimdemedes/ink) (React for CLI).

Features CJK/IME input support with grapheme-aware cursor positioning, real-time gRPC streaming, and a segment-based responsive status bar.

## Quick Start

```bash
cd tui
npm install
npm start         # Connects to gRPC server at localhost:50051
npm test          # 330+ unit tests (vitest)
npm run typecheck # TypeScript type checking
```

## Layout

Single-column vertical layout (v3):

```
╭─ UC v0.1.0 ──────────────────────────────────────────────╮
│ [12:00] ▎ fix bug              (user message, full width) │
│ [12:00] ⚙ Task created: abc1   (system message)          │
│ [12:00] 📋 1/3 ✅ │ 2 ⏳       (subtask summary inline)  │
│───────────────────────────────────────────────────────────│
│ > type task description and press Enter...                │
│ ◆ UC │ ● grpc │ P 0/3 │ F Input                         │
╰───────────────────────────────────────────────────────────╯
```

- ChatLog is full-width, single column
- SubtaskTree shown as overlay via Ctrl+T
- Subtask summary line rendered inline in ChatLog

Status bar uses a segment-based width budget: priority-ordered segments (brand > connection > worker > backend > progress > focus > retry > help) are added until the terminal width is exhausted. Less-critical info moved to the `?` help overlay.

## Keyboard Shortcuts

All shortcuts are defined in `keymap.ts` — the single source of truth ensuring "status bar says it, key does it."

### Global (work in any focus area)

| Key | Action |
|-----|--------|
| Shift+Tab | Cycle focus: input → chat → input |
| Esc | Context-dependent: close overlay / return to main / focus input |
| Ctrl+T | Toggle subtask overlay |
| Ctrl+F | Cycle event filter (all → task → subtask → tool → error) |
| Ctrl+P | Pause/resume current task |
| Ctrl+R | Reconnect gRPC |
| Ctrl+Q | Quit |
| ? | Show/hide keyboard shortcuts (not in input focus) |

### Input Focus

| Key | Action |
|-----|--------|
| Tab | Insert 2-space indent |
| Enter | Submit task |
| Ctrl+J | Insert newline (multi-line) |
| Ctrl+U | Clear entire input |
| Ctrl+K | Delete from cursor to end |
| ↑ / ↓ | Browse input history |

### Chat Focus

| Key | Action |
|-----|--------|
| ↑ / ↓ | Scroll up/down 1 line |
| PageUp / PageDown | Scroll up/down 1 page |
| Home | Jump to top (pause follow) |
| End | Jump to bottom (resume follow) |
| Enter | Expand/collapse long messages |
| Ctrl+L | Clear chat log |

### Subtask Overlay (Ctrl+T)

| Key | Action |
|-----|--------|
| ↑ / ↓ | Navigate previous/next subtask |
| Enter | Toggle subtask detail panel |
| R | Retry failed subtask |
| Esc | Close overlay |

### Subtask Detail View

When a subtask is selected and you press Enter, a detail panel expands below the row showing:
- Full description
- Status (with color)
- Assigned worker
- Dependencies (IDs)
- Error summary (for failed subtasks)

Press Enter again or Esc to close the detail.

### Input Indicators

| Indicator | Meaning |
|-----------|---------|
| `Ln2:Col8` | Multi-line: line 2, column 8 |
| `history 3/10` | Browsing input history: entry 3 of 10 |
| `[submitting...]` | Task submission in progress |
| `offline demo: type...` | gRPC not connected (offline mode) |

## Focus Model

The TUI uses a 2-area focus model:

- **focusedArea** (`input` | `chat`): which area receives keyboard events
- Shift+Tab cycles: input → chat → input
- SubtaskTree is accessed via the Ctrl+T overlay (not a focus area)

## Connection States

| State | Indicator | Color | Behavior |
|-------|-----------|-------|----------|
| connected | ● | Green (streaming) / Yellow (idle) | Full gRPC functionality |
| connecting | ◌ | Yellow | Auto-probe in progress |
| disconnected | ○ | Yellow | Offline demo mode |
| error | ✗ | Yellow | Offline demo, exponential backoff retry |

All non-connected states use **yellow** — offline is expected (development, no server), not an error. Only `connected + streaming` uses green. Press Ctrl+R to manually reconnect (deduplicated if already connecting).

## Cursor Strategy

The TUI hides the real terminal cursor (`\x1B[?25l`) and renders an inline inverse-video cursor indicator (`\x1B[7m...\x1B[27m`) via CjkTextInput. This avoids the dual-cursor problem where Ink's render cycle positions the real cursor unpredictably. The real cursor is restored on exit (`\x1B[?25h`).

IME candidate windows are positioned by the terminal near the last text output, which is close enough to the inline cursor for CJK composition.

## Testing

```bash
npm test              # Run all 330+ tests (vitest)
npm run test:watch    # Watch mode
npm run typecheck     # TypeScript type checking
```

### Testable vs Untestable Boundaries

Pure functions are tested with vitest (no React rendering needed):

| Module | Description |
|--------|-------------|
| `reducer.ts` | State transitions (67 tests) |
| `keymap.ts` | Command lookup + status bar help (9 tests) |
| `formatters.ts` | Event formatting (13 tests) |
| `symbols.ts` | Unicode/ASCII symbol resolution (9 tests) |
| `truncate.ts` | CJK-safe width truncation (8 tests) |
| `filter.ts` | Event filter logic (8 tests) |
| `cjk-input-utils.ts` | Grapheme editing operations (45 tests) |
| `statusbar-utils.ts` | Segment building + selection (17 tests) |
| `taskinput-utils.ts` | Input history + draft management (16 tests) |
| `offline-utils.ts` | Offline task decomposition (12 tests) |
| `chatlog-utils.ts` | Message formatting + scroll (9 tests) |

React hooks (`useGrpcClient`, `useTaskEvents`) and Ink components require React rendering and are tested separately.

## Architecture

### Core Modules

| Module | Role |
|--------|------|
| `reducer.ts` | Single source of truth — `useReducer` with 15+ action types |
| `keymap.ts` | Centralized keyboard command definitions + status bar help |
| `App.tsx` | Root layout + global keyboard handler + overlay interaction |
| `StatusBar.tsx` | Segment-based responsive status bar (`buildSegments` → `selectSegments`) |
| `ChatLog.tsx` | Message log with scrolling, filtering, unread count, expand/collapse |
| `SubtaskTree.tsx` | Subtask list with keyboard navigation + detail panel |
| `CjkTextInput.tsx` | CJK/IME-aware text input (delegates to `cjk-input-utils.ts`) |
| `TaskInput.tsx` | Task submission input with history + multi-line support |

### Pure Function Modules

| Module | Role |
|--------|------|
| `cjk-input-utils.ts` | Grapheme editing: insert, deleteBackward, deleteToEnd, renderInputWithCursor |
| `statusbar-utils.ts` | Segment building/selection for responsive layout |
| `taskinput-utils.ts` | History navigation, draft save/restore |
| `offline-utils.ts` | Simulated task decomposition for offline mode |
| `chatlog-utils.ts` | Timestamp formatting, message rendering, scroll math |
| `formatters.ts` | Proto event → display text conversion |
| `symbols.ts` | Unicode/ASCII/auto symbol set |
| `truncate.ts` | CJK-safe width-aware truncation |
| `filter.ts` | Event type filter logic |

### Hooks

| Hook | Role |
|------|------|
| `useGrpcClient.ts` | gRPC connection with exponential backoff (exported: `isUnavailableError`, `getErrorMessage`) |
| `useTaskEvents.ts` | Task event streaming + subtask state (exported: `processEvent`, `protoSubtasksToItems`) |
| `useCursor.ts` | Hide/show real terminal cursor (fake-only strategy) |

### gRPC Client

| Module | Role |
|--------|------|
| `grpc/client.ts` | Node.js gRPC client (proto-loader dynamic loading) |
| `grpc/types.ts` | TypeScript type definitions for proto messages |
