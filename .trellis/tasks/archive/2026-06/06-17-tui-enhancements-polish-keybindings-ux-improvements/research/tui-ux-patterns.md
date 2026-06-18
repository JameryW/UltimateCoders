# Research: Terminal TUI UX Patterns for Ink 5 + React

- **Query**: Best practices for terminal TUI user experience: status feedback, input UX, chat display, state feedback in Ink 5 + React TUI
- **Scope**: Mixed (internal codebase + external references from Ink, Gemini CLI, aider, ink-spinner, ink-scroll-view, cli-spinners)
- **Date**: 2026-06-17

## Findings

### 1. Status Feedback Patterns (Spinners, Progress Bars, Connection State)

#### Ink's `useAnimation` Hook (Preferred Over Manual `setInterval`)

Ink 5 provides a built-in `useAnimation` hook that is the recommended way to drive periodic re-renders for spinners, countdowns, and animations. Key properties:

- **`frame`**: Discrete counter incrementing each interval. Use for indexed spinner frames: `characters[frame % characters.length]`
- **`time`**: Total elapsed ms since animation start. Use for continuous math: `Math.sin(time / 1000 * Math.PI * 2)`
- **`delta`**: Time since previous tick. Use for physics/velocity: `position += speed * delta`
- **`reset()`**: Resets all values to 0 and restarts timing
- **`interval`**: Default 100ms, configurable
- **`isActive`**: Boolean to start/stop animation; toggling to `true` resets values

**Critical advantage**: All `useAnimation` instances share a single internal timer, consolidating multiple animated components into one render cycle. This avoids the "multiple setInterval timers causing staggered re-renders" problem.

Example from Ink docs:
```tsx
const Spinner = () => {
  const {frame} = useAnimation({interval: 80});
  const characters = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
  return <Text>{characters[frame % characters.length]}</Text>;
};
```

#### Current Implementation vs. Best Practice

**Current** (`StatusIndicator.tsx`): Uses manual `useState` + `setInterval` at 100ms for spinner frame and elapsed time. Two separate state updates per tick (`setFrame` and `setElapsed`).

**Best practice**: Replace with `useAnimation({interval: 100, isActive: isSubmitting || isStreaming})`:
- `frame` replaces manual `setFrame` counter
- `time` replaces manual `Date.now() - startedAt` elapsed calculation
- Single render cycle instead of two `setState` calls per tick
- Automatic cleanup (no `timerRef` / `clearInterval` needed)

#### Spinner Frame Conventions

From `cli-spinners` (used by ink-spinner, ora, Gemini CLI):
- **`dots`** (braille): 10 frames, 80ms interval -- most common, used by Gemini CLI
- **`dots2`/`dots3`**: 8-10 frames, 80ms -- alternative braille patterns
- **`line`**: 4 frames, 130ms -- simple line rotation
- **`circle`**: 8 frames, 120ms -- rotating circle quarters

Current project uses 10-frame braille at 100ms (close to standard `dots` at 80ms). The 80ms interval is more fluid; 100ms is acceptable but slightly slower.

#### Countdown Timer Patterns

For retry countdown display (e.g., "retry in 3s"), two approaches:

1. **Derive from `useAnimation().time`**: `const secondsLeft = Math.max(0, Math.ceil((nextRetryAt - Date.now() - time) / 1000))` -- but this requires knowing the absolute target time and the animation start time.

2. **Separate 1-second interval** (simpler): A `useState` counter that decrements each second via `setInterval(fn, 1000)`. This is lightweight and appropriate for a slow countdown that doesn't need sub-second precision.

3. **Gemini CLI approach**: The `useAnimatedScrollbar` hook uses `setInterval` at 33ms for smooth color interpolation with fade-in (200ms), visible (1000ms), fade-out (300ms) phases. This is a good pattern for any smooth transition in a status bar.

**For the StatusBar retry countdown**: The current code computes `retrySecondsLeft` in `getConnDetail()` using `Date.now()`, but this function is only called on render -- there is no periodic re-render to update the countdown. The fix is either:
- Add a `useAnimation({interval: 1000, isActive: isRetrying})` in StatusBar to force re-render every second
- Or pass `time` from a parent's `useAnimation` and derive the countdown

#### Connection State Change Feedback

Current implementation (in `App.tsx` lines 138-157): Uses `useEffect` on `connectionState` to detect transitions and add system messages. This is correct -- connection state changes are infrequent and message-based feedback is appropriate.

Gemini CLI pattern: Uses a `StreamingContext` with `StreamingState` enum (`Idle`, `Responding`, `Thinking`) and a `GeminiRespondingSpinner` that switches between spinner types based on state. The spinner also respects screen reader mode via `useIsScreenReaderEnabled()`.

### 2. Input UX (Multi-line Editing, CJK, Undo/Redo)

#### Multi-line Editing Conventions

**Aider** (prompt_toolkit based):
- Toggle mode: `/multi` command toggles multiline_mode
- In multiline mode: Enter inserts newline, Alt+Enter submits
- In normal mode: Enter submits, Alt+Enter inserts newline
- Ctrl+X Ctrl+E opens external editor (like Bash) for complex input
- Shows "multi>" prefix when in multiline mode
- Uses `EditingMode.EMACS` by default, `EditingMode.VI` optional
- Ctrl+Up/Down for history navigation (separate from Up/Down for cursor)

**Gemini CLI** (Ink-based):
- Newline bindings: `ctrl+enter`, `cmd+enter`, `alt+enter`, `shift+enter`, `ctrl+j`
- Submit: `enter` (single key)
- External editor: `ctrl+g` or `ctrl+shift+g`
- Uses a full `TextBuffer` with cursor row/col, visual layout, word navigation
- Supports word-boundary detection across scripts (Latin, Han, Arabic, Hiragana, Katakana, Cyrillic)
- Uses `string-width` for CJK display width calculations
- Uses `cpSlice`/`cpLen`/`toCodePoints` for code-point-aware operations

**Current project** (`CjkTextInput.tsx`):
- Ctrl+J for newline (matches Gemini CLI's least-preferred binding)
- Enter for submit
- No Alt+Enter / Shift+Enter for newline (these are more conventional)
- Uses `GraphemeSplitter` + `string-width` (correct approach)
- No undo/redo
- No word navigation (Ctrl+Left/Right, Alt+B/F)
- No external editor fallback
- No paste handling (Ink 5's `usePaste` hook not used)

#### CJK/IME Handling

**Ink 5's `useCursor` hook**: Provides `setCursorPosition({x, y})` to position the real terminal cursor for IME composition. Essential for CJK input where the composing character appears at the cursor location. Must use `string-width` for `x` calculation with wide characters.

**Ink 5's `usePaste` hook**: Handles bracketed paste mode (`\x1b[?2004h`). Pasted text arrives as a single string rather than individual key presses. `usePaste` and `useInput` operate on separate channels.

**Current project**: Uses custom `useCursor` hook (in `hooks/useCursor.ts`) that hides the real terminal cursor and renders an inline inverse-video cursor indicator. The `onCursorMove` callback in `CjkTextInput` is a no-op (commented out real ANSI cursor positioning due to dual-cursor issues).

**Gemini CLI**: Uses Ink's built-in `useCursor` for IME support with `setCursorPosition`. Also uses Kitty Keyboard Protocol (`useKittyKeyboardProtocol`) for enhanced key detection.

#### Undo/Redo Patterns

**Gemini CLI** (most complete implementation):
- `undoStack` and `redoStack` arrays in `TextBufferState`
- `historyLimit = 100` entries
- Snapshot includes: `lines`, `cursorRow`, `cursorCol`, `pastedContent`, `expandedPaste`
- `pushUndo`: Creates snapshot of current state, pushes to undoStack, clears redoStack
- `undo`: Pops from undoStack, pushes current state to redoStack, restores popped state
- `redo`: Pops from redoStack, pushes current state to undoStack, restores popped state
- Snapshots created before mutating operations (insert, delete, replace, etc.)
- Platform-aware keybindings:
  - macOS: Cmd+Z (undo), Cmd+Shift+Z (redo), Alt+Z (undo fallback)
  - Windows: Ctrl+Z, Alt+Z (undo); Ctrl+Shift+Z, Alt+Shift+Z (redo)
  - Linux: Alt+Z, Cmd+Z, Ctrl+Z (undo); Ctrl+Shift+Z, Cmd+Shift+Z, Alt+Shift+Z (redo)

**Current project**: No undo/redo implementation. Adding it would require:
1. An undo/redo stack in CjkTextInput state or reducer
2. Snapshot creation before each mutation
3. Platform-aware keybindings (Ctrl+Z conflicts with SIGTSTP on Linux)

#### Input History Navigation

**Gemini CLI** (`useInputHistory`):
- `historyCacheRef` stores text + cursor offset for each history level
- Level -1 = current unsubmitted prompt
- Saves cursor position when navigating away, restores when returning
- `previousHistoryIndexRef` tracks the index just before current, for "returning" detection
- Cursor restoration is conditional: only if cursor was not at first/last character

**Current project**: Simpler approach -- saves draft on first history navigation, restores on exit. No cursor position preservation in history.

### 3. Chat/Message Display (Scrolling, Folding, Timestamps)

#### Scrolling Patterns

**Gemini CLI** (`ScrollableList` + `VirtualizedList`):
- Full virtualized list: only renders visible items
- `estimatedItemHeight()` for layout calculation
- Smooth scrolling with animation: 200ms duration, 33ms frame interval
- `scrollBy`, `scrollTo`, `scrollToEnd`, `scrollToIndex`, `scrollToItem` imperative API
- `useAnimatedScrollbar`: Fade-in (200ms) -> visible (1000ms) -> fade-out (300ms) color transition
- Mouse wheel support via `MouseContext`
- Sticky headers for items (sticky + stickyChildren pattern)
- `initialScrollIndex: Number.MAX_SAFE_INTEGER` for "scroll to bottom on init"
- `alternateBuffer: true` in render options for full-screen mode
- Keyboard: Shift+Up/Down for scroll, Ctrl+Home/End for jump top/bottom

**ink-scroll-view** (third-party):
- `ScrollViewRef` with `scrollTo(offset)` and `scrollBy(delta)` imperative methods
- `onScroll`, `onViewportSizeChange`, `onContentHeightChange`, `onItemHeightChange` callbacks
- Virtualized rendering of visible items

**ink-virtual-list** (third-party):
- Virtualized list rendering only visible items for performance

**Current project** (`ChatLog.tsx`):
- Window slicing: `filteredMessages.slice(effectiveOffset, endIdx)`
- Manual scroll via `scrollCommand` with monotonically increasing `tick`
- `followLog` auto-follow with `maxOffset` calculation
- No virtualization (renders all visible messages as full components)
- No smooth scroll animation (instant jump)
- No mouse wheel support
- Scroll indicators: `↑N-M/T↓` format

#### Message Folding/Collapsing

**Current project** (`ChatLog.tsx`):
- `COLLAPSE_THRESHOLD = 3` lines
- Tool events (`tool_call`, `tool_result`, `file_modified`) always collapsed to 1 line
- Long non-tool messages (>3 lines) collapsed to 1 line with `[+N more]` hint
- Tool events show `[+N lines -- Enter to expand]`
- `expandAll` toggle via Enter in chat focus
- Per-message `expanded` state via `useState`

**Gemini CLI**: Uses markdown rendering with `ink-markdown`-style component. Tool calls shown as collapsible sections. No explicit line-count threshold visible in the components reviewed.

**Aider**: Uses Rich console with `Markdown` renderer. Tool output shown with syntax highlighting. No explicit folding mechanism visible.

#### Timestamp Formatting Conventions

**Current project**: `new Date().toTimeString().slice(0, 5)` -> `"12:00"` (HH:MM format)

**Common patterns across terminal apps**:
- **HH:MM** (current project): Compact, good for same-day sessions
- **HH:MM:SS**: More precise, useful for debugging
- **Relative time**: "2m ago", "just now" -- used in web chat UIs, less common in terminal
- **ISO 8601 date+time**: `2024-01-15 12:00` -- used for log files, not interactive display

The current HH:MM format is appropriate for a TUI. Adding seconds could be useful for debugging but adds visual noise.

### 4. State Feedback (Submission In-Progress, Stream Progress, Connection Health)

#### Compact Status Bar Patterns

**Current project** (`StatusBar.tsx`):
- Segment-based layout with width budget
- Priority order: brand > connection > worker > backend > progress > focus > retry > help
- Progressive collapse tiers: >100 cols (full), 80-100 (remove help), 60-80 (brand+connection+progress+focus), <60 (brand+connection+progress)
- Connection indicators: `●` connected (green streaming, yellow idle), `○` disconnected, `◌` connecting, `✗` error
- Retry display: `retry N/5` when error + retrying, `C-R reconnect` when disconnected
- Focus indicator: `F Input` / `F Chat`

**Vim airline / Powerline pattern**: Left side = mode + file info, Right side = git + encoding. Segments separated by color-coded dividers. Progressive truncation from right.

**tmux status bar pattern**: Left = session info, Right = clock + host. `[session:window]` format. Color indicates active/inactive.

**Gemini CLI**: Uses semantic color theming (`theme.text.primary`, `theme.ui.dark`). Status shown via spinner states (idle/thinking/responding). No explicit status bar -- the spinner IS the status indicator.

#### Submission In-Progress Feedback

**Current project** (`StatusIndicator.tsx`):
- Shows `⠋ Working... (5s)  Esc cancel` when submitting
- Shows `⠋ Streaming... (1m 30s)` when streaming
- `formatElapsed()`: `(5s)` / `(1m 30s)` / `(2h 15m 00s)` format
- Disappears when neither submitting nor streaming

**Best practices**:
- Show elapsed time (current project does this correctly)
- Show cancel hint when cancellation is possible (current project does this)
- Distinguish between "submitting" (waiting for server response) and "streaming" (receiving events)
- Consider adding a progress bar for known-total operations (e.g., subtask completion)

#### Stream Progress Feedback

**Current project**: `isStreaming` boolean drives spinner. No byte/token count or progress percentage.

**Gemini CLI**: `StreamingState` enum: `Idle`, `Responding`, `Thinking`. Different spinner for each state. The `GeminiRespondingSpinner` uses a rainbow spinner during responding.

**Enhancement options**:
- Token count: "Streaming... (1.2k tokens)"
- Subtask progress bar: `[████░░░░░░] 4/10` using symbols from `symbols.ts` (`barFilled`/`barEmpty`)
- Throughput indicator: "Streaming... (120 tok/s)"

#### Connection Health in Status Bar

**Current project**: Dot + label (`● grpc` / `○ offline` / `◌ offline` / `✗ offline`). Retry countdown computed but not periodically re-rendered.

**Best practice for retry countdown**: Add a `useAnimation({interval: 1000, isActive: connectionState === 'error' && retryCount > 0})` in StatusBar to force re-render every second, so the countdown updates visually.

### Files Found

| File Path | Description |
|---|---|
| `tui/src/components/App.tsx` | Root layout, keyboard handler, state management |
| `tui/src/components/StatusIndicator.tsx` | Spinner + elapsed time display |
| `tui/src/components/StatusBar.tsx` | Segment-based status bar with width budget |
| `tui/src/components/TaskInput.tsx` | Input wrapper with history, multiline indicators |
| `tui/src/components/CjkTextInput.tsx` | CJK-aware text input with grapheme editing |
| `tui/src/components/ChatLog.tsx` | Message log with window slicing, folding, filtering |
| `tui/src/reducer.ts` | Central state management (TuiState + tuiReducer) |
| `tui/src/keymap.ts` | Centralized keyboard command definitions |
| `tui/src/cjk-input-utils.ts` | Pure functions for grapheme-aware editing |
| `tui/src/formatters.ts` | Event-to-message formatting with markdown |
| `tui/src/symbols.ts` | Unicode/ASCII symbol strategy |
| `tui/src/statusbar-utils.ts` | Connection indicator, layout mode helpers |
| `tui/src/hooks/useGrpcClient.ts` | gRPC connection with exponential backoff |
| `tui/src/offline-utils.ts` | Offline simulation helpers |
| `tui/package.json` | Dependencies: Ink 5.2, React 18.3, grapheme-splitter, string-width |

### Code Patterns

#### Periodic Re-render Pattern (Current)
In `StatusIndicator.tsx:39-62`: Manual `setInterval` + `useState` for spinner frame and elapsed time. Two state updates per tick. Cleanup via `useEffect` return.

#### Periodic Re-render Pattern (Recommended)
Use Ink's `useAnimation({interval, isActive})` hook. Single render cycle for all animated components. No manual cleanup.

#### Segment-Based Status Bar (Current)
In `StatusBar.tsx:59-257`: `buildSegments()` creates ordered segments with width, `selectSegments()` applies progressive collapse tiers based on terminal width budget.

#### CJK Input (Current)
In `CjkTextInput.tsx`: `GraphemeSplitter` for cluster-aware editing, `string-width` for display width, inverse-video cursor indicator, `insertAtCursor`/`deleteBackward`/`deleteToEnd` pure functions.

#### Message Folding (Current)
In `ChatLog.tsx:96-97`: `COLLAPSE_THRESHOLD = 3`, `TOOL_EVENT_TYPES` set. Per-message `expanded` state. `expandAll` toggle for batch expand/collapse.

#### Undo/Redo (Not Implemented)
No undo/redo stack exists in the current codebase. Gemini CLI's `TextBuffer` pattern with `undoStack`/`redoStack` (limit 100) is the reference implementation.

### External References

- [Ink 5 README](https://github.com/vadimdemedes/ink/blob/master/readme.md) -- `useAnimation`, `useCursor`, `usePaste`, `useWindowSize`, `<Static>` component docs
- [ink-spinner](https://github.com/vadimdemedes/ink-spinner) -- Spinner component using `useState` + `setInterval` (pre-useAnimation pattern)
- [cli-spinners](https://github.com/sindresorhus/cli-spinners) -- Spinner frame data (dots: 10 frames @ 80ms)
- [ink-text-input](https://github.com/vadimdemedes/ink-text-input) -- Reference text input with cursor offset, paste highlighting, mask support
- [ink-scroll-view](https://github.com/ByteLandTechnology/ink-scroll-view) -- Virtualized scroll container with imperative API
- [ink-virtual-list](https://github.com/archcorsair/ink-virtual-list) -- Virtualized list for performance
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) -- Full Ink-based TUI with: `TextBuffer` (undo/redo, word navigation, script-aware boundaries), `ScrollableList` (virtualized, smooth scroll, animated scrollbar), `keyBindings.ts` (data-driven, user-configurable, platform-aware), `useInputHistory` (cursor-preserving), `useAnimatedScrollbar` (fade in/out)
- [Aider](https://github.com/Aider-AI/aider) -- prompt_toolkit-based TUI with: multiline toggle mode, Ctrl+X Ctrl+E external editor, Emacs/Vi editing modes, Rich markdown rendering

### Related Specs

- `.trellis/spec/frontend/component-guidelines.md` -- Component structure guidelines
- `.trellis/spec/frontend/state-management.md` -- State management patterns
- `.trellis/spec/frontend/hook-guidelines.md` -- Hook patterns
- `.trellis/spec/frontend/tui-grpc-spec.md` -- TUI gRPC integration spec

## Caveats / Not Found

- **Claude Code source**: The repository at `anthropics/claude-code` does not expose its internal TUI component source code publicly. Patterns are inferred from behavior and the Ink dependency listing.
- **Ink `useAnimation` availability**: Confirmed in Ink 5 README (master branch). The current project uses `ink: ^5.2.0` which should include this hook, but it was not verified by reading `node_modules/ink`.
- **Ink `<Static>` component**: Documented in Ink README for permanently rendered output (completed items, logs). Not currently used in the project. Could improve performance by separating static (completed messages) from dynamic (active spinner) render trees.
- **Ink `alternateBuffer`**: Gemini CLI uses `render(<App />, { alternateBuffer: true })` for full-screen mode. This clears the terminal on entry/exit. Not currently used in the project.
- **Mouse support**: Gemini CLI uses mouse wheel for scrolling via `MouseContext`. Ink 5 does not natively support mouse events. Requires `enableMouseEvents()` / `disableMouseEvents()` and a custom context.
- **Kitty Keyboard Protocol**: Gemini CLI uses `useKittyKeyboardProtocol` for enhanced key detection (more key names, better modifier detection). Not available in base Ink 5.
- **Undo/Redo keybinding conflict**: Ctrl+Z sends SIGTSTP (suspend) on Linux/macOS terminals. Gemini CLI uses Alt+Z as primary undo on Linux to avoid this. The current project does not handle this.
