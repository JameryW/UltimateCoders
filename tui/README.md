# UltimateCoders TUI

Terminal UI for the UltimateCoders distributed AI coding system, built with [Ink](https://github.com/vadimdemedes/ink) (React for CLI).

## Quick Start

```bash
cd tui
npm install
npm start
```

## Layout

```
╭─ UC UltimateCoders v0.1.0 ● connected ──────────────────╮
│ Chat                    │ Subtasks [0/3 0%]            │
│ [12:00] > fix bug      │ ◉ 1. Analyze                 │
│ [12:00] Task created   │ ○ 2. Fix                     │
│                         │ ○ 3. Test                    │
│─────────────────────────────────────────────────────────│
│ > type task description and press Enter...             │
│ Focus:Input│View:Chat│ Progress:0/3│ ...              │
╰─────────────────────────────────────────────────────────╯
```

- **Wide (≥100 cols)**: Chat + Subtasks dual pane
- **Medium (80-99 cols)**: Dual pane, compressed right
- **Narrow (<80 cols)**: Single pane, shows activeMainPane

## Keyboard Shortcuts

### Global (work in any focus area)

| Key | Action |
|-----|--------|
| Shift+Tab | Cycle focus: input → chat → subtask → input |
| Esc | Context-dependent: close detail / return to main / focus input |
| Ctrl+W | Swap main pane (chat ↔ subtask) |
| Ctrl+F | Cycle event filter (all → task → subtask → tool → error) |
| Ctrl+P | Pause/resume current task |
| Ctrl+R | Reconnect gRPC |
| Ctrl+Q | Quit |
| ? | Show/hide keyboard shortcuts |

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
| Ctrl+L | Clear chat log |

### Subtask Focus

| Key | Action |
|-----|--------|
| ↑ / ↓ | Select previous/next subtask |
| Home / End | Jump to first/last subtask |
| f | Jump to next failed subtask |
| Enter | Toggle subtask detail panel |
| Ctrl+T | Retry failed subtask (coming soon) |

## Focus Model

The TUI uses a split focus model:

- **focusedArea** (`input` | `chat` | `subtask`): which area receives keyboard events
- **activeMainPane** (`chat` | `subtask`): which pane occupies the main area in narrow mode

These are independent — the input is always visible, and the main area always shows content regardless of which area has focus.

## Connection States

| State | Indicator | Behavior |
|-------|-----------|----------|
| connected | ● (green) | Full gRPC functionality |
| connecting | ◌ (yellow) | Auto-probe in progress |
| error | ✗ (red) | Offline demo mode, exponential backoff retry |
| disconnected | ○ (red) | Initial state |

When gRPC is unavailable, the TUI operates in offline demo mode with simulated task decomposition. Press Ctrl+R to manually reconnect.

## Testing

```bash
npm test              # Run all tests
npm run test:watch    # Watch mode
```

## Architecture

- `reducer.ts` — Single source of truth (useReducer)
- `keymap.ts` — Centralized keyboard command definitions
- `App.tsx` — Root layout + global keyboard handler
- `ChatLog.tsx` — Message log with scrolling, filtering, unread count
- `SubtaskTree.tsx` — Subtask list with keyboard navigation
- `CjkTextInput.tsx` — CJK-aware text input with grapheme support
- `useGrpcClient.ts` — gRPC connection with exponential backoff
