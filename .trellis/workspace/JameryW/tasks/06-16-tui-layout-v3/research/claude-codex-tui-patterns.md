# Research: Claude Code & Codex CLI TUI Design Patterns

- **Query**: TUI design patterns of Claude Code CLI and OpenAI Codex CLI - layout, panes, visual elements, streaming, tool output, error display, progress indicators
- **Scope**: mixed (external documentation + source code analysis)
- **Date**: 2026-06-16

## Findings

### 1. Claude Code CLI TUI Layout

#### Overall Architecture

Claude Code uses **Ink** (React-for-terminal) as its TUI framework. The CLI is a compiled binary (no public source for the TUI layer; the GitHub repo contains only plugins/examples). Key component names extracted from the minified bundle:

- `AssistantMessage` / `UserMessage` / `SystemMessage` / `ErrorMessage` - Message type components
- `ToolUseMessage` / `ToolResultMessage` / `ToolUseBlock` / `ToolResultBlock` - Tool call visualization
- `ThinkingBlock` / `TextBlock` - Content block types
- `CodeBlock` / `SyntaxHighlight` - Code rendering
- `MessageList` - Conversation scroll container
- `PermissionPrompt` / `PermissionRequest` / `ToolPermission` - Permission dialogs
- `TokenCounter` / `ProgressBar` - Usage indicators
- `StreamingText` / `CommandOutput` / `FileEdit` - Live content renderers

#### Layout Structure (Inferred from docs + source)

The layout is a **single-column, vertically stacked** design (no split panes in CLI mode):

```
┌──────────────────────────────────────────────┐
│  [Logo/Branding at session start]            │
│                                              │
│  ── Conversation Area (scrollable) ──────    │
│  User message (dim background tint)          │
│  Assistant message (markdown rendered)       │
│    ├── Thinking indicator: ∴ Thinking / ✻   │
│    ├── Tool call: ⏺ ToolName(args)          │
│    │   ├── collapsed: one-line summary       │
│    │   └── expanded: full output (click)     │
│    ├── Diff display (file edits)             │
│    │   ├── truncation at 400 line limit      │
│    │   └── "diff truncated (exceeded ...)"   │
│    └── Tool result with exit code            │
│  Permission prompt (inline overlay)          │
│  Error display (with --debug flag)           │
│                                              │
│  ── Status Indicator (when active) ──────    │
│  ∴ Thinking... / Working / Searching…        │
│                                              │
│  ── Input Area (fixed at bottom) ─────────   │
│  > [prompt input with cursor]               │
│  [suggestion ghost text in gray]            │
│  [vim mode indicator: -- INSERT --]         │
│                                              │
│  ── Status Line (customizable) ──────────    │
│  [model] [context %] [cost] [git branch]    │
└──────────────────────────────────────────────┘
```

#### Two Rendering Modes

1. **Classic (scrollback)**: Output writes to terminal scrollback. `Cmd+F` and native search work. Input box moves down as output streams in.
2. **Fullscreen (alternate screen)**: Uses terminal alternate screen buffer (like `vim`). Input stays **fixed at bottom**. Only visible messages kept in render tree (flat memory). Mouse support (click-to-expand, text selection, URL clicking). Toggle: `/tui fullscreen` or `CLAUDE_CODE_NO_FLICKER=1`.

#### Key Visual Elements

**Indicators and symbols:**
- `∴ Thinking` / `✻ Thinking…` - Extended thinking indicator
- `✻ Conversation compacted` - Context compaction notice
- `●` - Active/running indicator
- `✓` - Success (green)
- `✗` - Failure (red)
- `⧈` - Sandbox blocked
- `⧉` - File/link reference
- `└` - Tree continuation
- `▶` / `◀` - Navigation arrows
- `⚠️` - Warning

**Permission modes (shown in status bar):**
- `default` → `acceptEdits` → `plan` → `auto` (cycle with Shift+Tab)
- Mode indicator: `⏵⏵ accept edits on` for acceptEdits
- Plan mode: `Entered plan mode`, `✓ Plan Approved by`, `✗ Plan Rejected by`

**Thinking display:**
- Shows `∴ Thinking` or `✻ Thinking…` with animated indicator
- Extended thinking content can be toggled with `Alt+T`

**Tool output visualization:**
- Tool calls collapsed by default to one-line summary (e.g., `Called slack 3 times`)
- Expandable via `Ctrl+O` (transcript viewer) or mouse click (fullscreen)
- Diffs truncated at 400 line limit with notice
- File paths are clickable links (open in editor)
- Syntax highlighting in code blocks with toggle (`Ctrl+T` in `/theme`)

**Streaming:**
- Text streams character-by-character as it arrives
- Tool calls appear as they start, with live output
- Status indicator shows activity during streaming

**Error display:**
- Inline with conversation (not a separate pane)
- `※ Error logs shown inline with --debug`
- `※ Run claude --debug to see error logs`
- Connection errors: `✗ Connection error`, `✗ Failed to connect`

**Input area features:**
- Multi-line input via `\` + Enter, Shift+Enter, Ctrl+J, or Option+Enter
- Vim editor mode (NORMAL/INSERT/VISUAL)
- Reverse history search (Ctrl+R)
- Autocomplete: `/` for commands, `@` for files, `!` for shell mode
- Prompt suggestions (ghost text in gray, accept with Tab)
- Image paste (Ctrl+V) shows `[Image #N]` chip
- Ctrl+G opens external editor

**Status line (customizable, bottom of screen):**
- Runs any shell script, receives JSON session data on stdin
- Available data: model, context window %, cost, git branch, rate limits, session ID, etc.
- Supports multi-line, ANSI colors, OSC 8 hyperlinks
- Hides during autocomplete, help menu, permission prompts

**Transcript viewer (Ctrl+O):**
- Shows detailed tool usage and execution
- Expands MCP calls (collapsed to one-line by default)
- In fullscreen: less-style navigation (`/` search, `j`/`k` scroll, `g`/`G` jump)
- Write to native scrollback with `[`, open in editor with `v`

### 2. OpenAI Codex CLI TUI Layout

#### Overall Architecture

Codex CLI uses **Ratatui** (Rust TUI framework) with **Crossterm** backend. The TUI source lives in `codex-rs/tui/` with a modular architecture. Key structural components:

- `chatwidget.rs` - Main chat surface (owns history cells, active cell, bottom pane)
- `bottom_pane/` - Interactive footer (composer + popup views)
- `history_cell/` - Transcript/history display units
- `exec_cell/` - Tool/command output rendering
- `streaming/` - In-flight stream state and markdown collection
- `status_indicator_widget.rs` - Live task status row
- `render/` - Rendering primitives (highlight, layout, renderable trait)
- `markdown_render.rs` - Markdown event renderer (pulldown-cmark → ratatui lines)
- `diff_render.rs` - Unified diff renderer with syntax highlighting

#### Layout Structure (from source code analysis)

```
┌──────────────────────────────────────────────┐
│  [Welcome Screen / ASCII Art on first run]   │
│  "Welcome to Codex, OpenAI's command-line    │
│   coding agent"                               │
│                                              │
│  ── Transcript Area (flex-grow) ──────────   │
│  [HistoryCell: committed entries]            │
│  [ActiveCell: in-flight streaming cell]      │
│    ├── User message (tinted background)      │
│    ├── Assistant message (markdown)           │
│    │   ├── Code blocks (syntax highlighted)  │
│    │   ├── Tables (auto-layout/column-aware) │
│    │   └── Links (terminal hyperlinks)       │
│    ├── Exec cell (command output)            │
│    │   ├── command line (bash highlighted)   │
│    │   ├── output (dimmed, with └ prefix)   │
│    │   ├── truncated at TOOL_CALL_MAX_LINES  │
│    │   │   (5 lines default, 50 for shell)  │
│    │   └── "ctrl + t to view transcript"     │
│    ├── Diff display (unified diff)           │
│    │   ├── Line numbers + gutter signs       │
│    │   ├── Add: green bg / Delete: red bg    │
│    │   ├── Theme-aware (dark vs light)       │
│    │   └── Syntax highlighted per-hunk       │
│    ├── Approval overlay (permission prompt)  │
│    ├── Hook cell (hook execution display)    │
│    ├── Plan cell (plan steps display)        │
│    └── MCP cell (MCP tool call display)      │
│                                              │
│  ── Status Indicator (above composer) ────   │
│  Working [12s] · Esc to interrupt            │
│    └─  └ Detail line (indented tree style)  │
│                                              │
│  ── Bottom Pane (flex-shrink: 0) ─────────   │
│  [ChatComposer: textarea input]              │
│  [Footer: mode indicator, shortcuts, etc.]   │
│    ├── Plan mode / Pair Programming / Execute│
│    ├── "shift+tab to cycle"                  │
│    ├── "? for shortcuts"                     │
│    └── Status line (customizable)            │
└──────────────────────────────────────────────┘
```

#### Rendering Composition (from `chatwidget/rendering.rs`)

The layout uses a **FlexRenderable** system (custom ratatui layout):
- Active cell: `flex=1` (grows to fill space)
- Active hook cell: `flex=0` (shrinks to content)
- Pending token activity: `flex=1` (when streaming)
- Bottom pane: `flex=0` (fixed height, with `inset(top=1)` for separator)

Only **visible messages** are kept in the render tree (viewport-based virtualization).

#### Key Visual Elements

**Status indicator widget (from `status_indicator_widget.rs`):**
- Single-line status: `Working [12s] · Esc to interrupt`
- Animated spinner with shimmer effect (truecolor) or blink (limited color)
- Header text: "Working", "Reviewing approval request", etc.
- Details text below: indented with `└` prefix, max 3 lines
- Inline message suffix after elapsed/interrupt segment
- Reduced motion mode: static bullet `•` or hidden

**Chat composer (from `chat_composer.rs`):**
- TextArea-based input with vim mode support
- Slash commands: `/` prefix with popup selection
- File search: `@` mention with popup
- Skills/apps mentions
- Multi-line via Shift+Enter / Ctrl+J
- History navigation (Up/Down) with persistent + session history
- Ctrl+R reverse incremental search
- Large paste placeholders: `[Pasted Content N chars]`
- Image attachments: `[Image #N]` rows (non-editable)
- Tab to queue submission while task running

**Footer (from `footer.rs`):**
- `FooterMode` enum: HistorySearch, QuitShortcutReminder, ShortcutOverlay, EscHint, ComposerEmpty, ComposerHasDraft
- Collaboration mode indicators: Plan (magenta), Pair Programming (cyan), Execute (dim)
- Goal status indicator: Active/Paused/Blocked/UsageLimited/BudgetLimited/Complete
- Width-responsive collapse: drops elements when terminal is narrow
- Single-line footer with width-based fallback rules

**History cell types (from `history_cell/mod.rs`):**
- `approvals` - Approval request/response cells
- `base` - Base cell trait
- `exec` - Command execution cells
- `hook_cell` - Hook execution cells
- `mcp` - MCP tool call cells
- `messages` - User/assistant message cells
- `notices` - System notice cells
- `patches` - File diff/patch cells
- `plans` - Plan step cells
- `request_user_input` - User input request cells
- `search` - Search result cells
- `separators` - Visual separators between turns
- `session` - Session-level cells

**Streaming (from `streaming/mod.rs`):**
- `StreamState`: newline-gated markdown collection + FIFO queue of committed render lines
- `MarkdownStreamCollector`: collects streaming markdown events
- Queue-based drain: oldest-first, with age tracking for policy decisions
- Controller adapts queued lines into HistoryCell emission rules
- Chunking computes adaptive drain plans from queue pressure
- commit_tick binds policy decisions to concrete controller drains

**Diff rendering (from `diff_render.rs`):**
- Unified diffs with line numbers and gutter signs (`+`/`-`/` `)
- Syntax highlighting per-hunk (preserves parser state across lines)
- Theme-aware backgrounds:
  - Dark: `#213A2B` (add), `#4A221D` (del)
  - Light: `#dafbe1` (add), `#ffebe9` (del) - GitHub-style
- Palette-aware: truecolor, 256-color, 16-color fallbacks
- Syntax theme scope backgrounds override hardcoded palette

**Markdown rendering (from `markdown_render.rs`):**
- pulldown-cmark event consumption → styled ratatui lines
- Table rendering pipeline: filter spillover → normalize columns → allocate widths → choose presentation (row-separated or key/value transposed)
- Column classification: Narrative, TokenHeavy, Compact
- Width-aware wrapping
- Heading styles (h1-h6), code, emphasis, strong, strikethrough
- Local file links display destination (not label)
- Web URLs annotated with terminal hyperlinks

**Color and theme (from `style.rs` + `color.rs`):**
- Light/dark background detection: luminance > 128 = light
- Color blending with alpha compositing
- Perceptual color distance (CIE76/Lab space)
- User message: blended background tint (4% black on light, 12% white on dark)
- Accent style: cyan on dark, blended dark-cyan on light
- Terminal palette detection: TrueColor, Ansi256, Ansi16, Unknown
- Reduced motion: shimmer → static, blink → bullet

### 3. Common Patterns Between Claude Code and Codex CLI

| Pattern | Claude Code | Codex CLI |
|---|---|---|
| **Framework** | Ink (React) + Node.js | Ratatui (Rust) + Crossterm |
| **Rendering** | React component tree → terminal | Custom `Renderable` trait → ratatui `Buffer` |
| **Layout** | Single-column vertical (Ink Box/Text) | Flex-based (FlexRenderable) vertical stack |
| **Viewport model** | Fullscreen: alternate screen buffer, virtualized rendering | Always alternate screen, viewport-based virtualization |
| **Input area** | Fixed at bottom (fullscreen mode) | Fixed at bottom (bottom_pane, flex=0) |
| **Status indicator** | `∴ Thinking` / `✻ Thinking…` / `Searching…` | `Working [12s] · Esc to interrupt` with shimmer |
| **Permission prompts** | Inline overlay, mode cycling (Shift+Tab) | Approval overlay in bottom pane |
| **Tool output** | Collapsed by default, expandable via click/Ctrl+O | Collapsed (5 lines), expand via Ctrl+T transcript |
| **Diff display** | Truncated at 400 lines, syntax highlighted | Unified diff, theme-aware bg, per-hunk highlighting |
| **Markdown** | Custom renderer (Ink) | pulldown-cmark → ratatui lines, table-aware |
| **Streaming** | Ink re-render on state change | Queue-based drain with chunking policy |
| **Status line** | Customizable shell script (JSON on stdin) | Customizable status-line items |
| **History search** | Ctrl+R reverse search | Ctrl+R reverse incremental search |
| **Vim mode** | Normal/Insert/Visual | Normal/Insert (textarea vim) |
| **Thinking display** | `∴ Thinking` / `✻ Thinking…` animated | Not visible (internal) |
| **Error display** | Inline with --debug flag | Inline in history cells |
| **Compaction** | `✻ Conversation compacted` notice | Context window management (not visible) |
| **Mouse support** | Fullscreen mode: click, select, scroll, URL | Not explicitly documented |
| **Session header** | Model name in status line | `SessionHeader` with model name |
| **Welcome screen** | Logo/banner at session start | ASCII art animation + "Welcome to Codex" |
| **Color adaptation** | Ink color names (green, red, etc.) | Light/dark detection, palette-aware rendering |
| **Reduced motion** | Not documented | Explicit `MotionMode::Reduced` fallback |

### 4. Streaming Output Patterns

**Claude Code:**
- Text streams character-by-character as API chunks arrive
- Ink re-renders on every state change (React reconciliation)
- Tool calls appear immediately when started, with live output appended
- Fullscreen mode: only visible messages in render tree, flat memory usage
- Compaction happens automatically when context fills (`✻ Conversation compacted`)

**Codex CLI:**
- `StreamState` collects markdown events via `MarkdownStreamCollector`
- Committed lines queued in FIFO with arrival timestamps
- Drain policy: oldest-first, with queue pressure-based adaptive chunking
- `commit_tick` binds policy to concrete drain timing
- Controller adapts queued lines into HistoryCell emission rules
- Active cell mutates in-place during streaming, becomes committed cell when done

### 5. Tool Call Visualization Patterns

**Claude Code:**
- `ToolUseMessage` → `ToolUseBlock`: shows tool name and arguments
- `ToolResultMessage` → `ToolResultBlock`: shows result with exit code
- Collapsed by default: `Called slack 3 times` (MCP), or one-line summary
- Expand via Ctrl+O (transcript viewer) or mouse click (fullscreen)
- Diff output shown inline with file path links
- Sandbox indicator: `⧈ Sandbox blocked`, `○ Sandbox disabled`, `✓ Sandbox enabled`

**Codex CLI:**
- `exec_cell`: command line (bash-highlighted) + output (dimmed, `└` prefix)
- Output truncated at `TOOL_CALL_MAX_LINES` (5) or `USER_SHELL_TOOL_CALL_MAX_LINES` (50)
- Truncation hint: `ctrl + t to view transcript`
- `patches`: file change display with unified diff
- `mcp`: MCP tool call with status
- `hook_cell`: hook execution display
- `plans`: plan step status display
- `approvals`: approval request/response overlay

### 6. Error Display Patterns

**Claude Code:**
- Errors rendered inline in conversation (not separate pane)
- `✗ Connection error`, `✗ Failed to connect`, `✗ Auto-update failed`
- `※ Error logs shown inline with --debug`
- `※ Run claude --debug to see error logs`
- Hook errors: `hook error:`, `hook returned blocking error`

**Codex CLI:**
- Errors as `notices` history cell type
- Command exit codes shown in exec cell
- Error output (stderr) rendered dimmed
- Guardian review errors shown in status indicator

### 7. Progress Indicator Patterns

**Claude Code:**
- `∴ Thinking` (static) / `✻ Thinking…` (animated) during extended thinking
- `Searching…` during search operations
- `Checking git status` during git operations
- `Loading conversations…` / `Loading stats…` during data fetch
- No explicit progress bar for token usage (shown in status line as %)
- Background task tracking with Ctrl+B

**Codex CLI:**
- `StatusIndicatorWidget`: animated spinner with shimmer (truecolor) or blink
- Header: `Working`, `Reviewing approval request`, custom labels
- Elapsed timer: `0s`, `59s`, `1m 00s`, `1h 00m 00s`
- Interrupt hint: `Esc to interrupt`
- Details below header: indented with `└` prefix, max 3 lines
- Reduced motion: static bullet `•` or hidden indicator
- `shimmer_text()` for animated text effects

### Files Found (Claude Code)

| File Path | Description |
|---|---|
| `cli.js` (minified, in Trae extension) | Main TUI bundle (14,766 lines minified) |
| `walkthrough/step1-4.md` | VS Code extension walkthrough |
| `AcceptMode.jpg`, `PlanMode.jpg` | Screenshots of permission modes |
| `claude-logo*.svg`, `clawd.svg` | Logo assets |

### Files Found (Codex CLI)

| File Path | Description |
|---|---|
| `codex-rs/tui/src/chatwidget.rs` | Main chat surface (protocol events, history cells, rendering) |
| `codex-rs/tui/src/chatwidget/rendering.rs` | Flex-based render composition |
| `codex-rs/tui/src/chatwidget/streaming.rs` | Stream lifecycle management |
| `codex-rs/tui/src/chatwidget/status_state.rs` | Status indicator state machine |
| `codex-rs/tui/src/chatwidget/session_header.rs` | Session header (model name) |
| `codex-rs/tui/src/chatwidget/footer.rs` | Footer rendering with width-responsive collapse |
| `codex-rs/tui/src/bottom_pane/mod.rs` | Bottom pane (composer + popup stack) |
| `codex-rs/tui/src/bottom_pane/chat_composer.rs` | Chat input state machine |
| `codex-rs/tui/src/bottom_pane/footer.rs` | Footer props and mode-based rendering |
| `codex-rs/tui/src/bottom_pane/approval_overlay.rs` | Permission/approval overlay |
| `codex-rs/tui/src/history_cell/mod.rs` | Transcript history cell types |
| `codex-rs/tui/src/exec_cell/render.rs` | Tool/command output rendering |
| `codex-rs/tui/src/streaming/mod.rs` | Streaming state (markdown collector + FIFO queue) |
| `codex-rs/tui/src/status_indicator_widget.rs` | Live task status row |
| `codex-rs/tui/src/markdown_render.rs` | Markdown → ratatui lines renderer |
| `codex-rs/tui/src/diff_render.rs` | Unified diff renderer |
| `codex-rs/tui/src/style.rs` | Color/theme styles (light/dark adaptive) |
| `codex-rs/tui/src/color.rs` | Color blending, luminance, perceptual distance |
| `codex-rs/tui/src/motion.rs` | Activity indicator (shimmer/blink/reduced) |
| `codex-rs/tui/src/token_usage.rs` | Token usage model and formatting |
| `codex-rs/tui/src/onboarding/welcome.rs` | Welcome screen with ASCII art |
| `codex-rs/tui/src/tui.rs` | Terminal initialization (alternate screen, raw mode) |
| `codex-rs/tui/src/keymap.rs` | Keybinding configuration |
| `codex-rs/tui/frames/codex/` | ASCII art animation frames |

### Files Found (Existing Project TUI)

| File Path | Description |
|---|---|
| `tui/src/components/App.tsx` | Root layout (split-pane: Chat + Subtasks) |
| `tui/src/components/ChatLog.tsx` | Chat message list |
| `tui/src/components/StatusBar.tsx` | Status bar |
| `tui/src/components/StatusIndicator.tsx` | Status indicator |
| `tui/src/components/SubtaskTree.tsx` | Subtask tree display |
| `tui/src/components/TaskInput.tsx` | Task input |
| `tui/src/components/CjkTextInput.tsx` | CJK-aware text input |
| `tui/src/components/LogoBanner.tsx` | Logo banner |
| `tui/src/symbols.ts` | Unicode symbol constants |
| `tui/src/statusbar-utils.ts` | Status bar utility functions |
| `tui/src/keymap.ts` | Keybinding definitions |
| `tui/src/reducer.ts` | State reducer |

### External References

- [Claude Code GitHub Repo](https://github.com/anthropics/claude-code) - Plugins only; TUI source is proprietary
- [Claude Code Documentation](https://code.claude.com/docs/llms.txt) - Full docs index
- [Claude Code Interactive Mode](https://code.claude.com/docs/en/interactive-mode.md) - Keyboard shortcuts, input modes, vim mode
- [Claude Code Fullscreen Rendering](https://code.claude.com/docs/en/fullscreen.md) - Alternate screen buffer mode
- [Claude Code Status Line](https://code.claude.com/docs/en/statusline.md) - Customizable status bar
- [Claude Code Permission Modes](https://code.claude.com/docs/en/permission-modes.md) - Mode cycling, approval prompts
- [Claude Code Output Styles](https://code.claude.com/docs/en/output-styles.md) - Proactive, Explanatory, Learning modes
- [Codex CLI GitHub Repo](https://github.com/openai/codex) - Full open-source TUI in `codex-rs/tui/`
- [Ratatui Framework](https://github.com/ratatui-org/ratatui) - Rust TUI framework used by Codex
- [Ink Framework](https://github.com/vadimdemedes/ink) - React-for-terminal used by Claude Code

### Related Specs

- `.trellis/spec/frontend/tui-grpc-spec.md` - TUI gRPC client & hooks spec
- `.trellis/spec/frontend/component-guidelines.md` - Component guidelines

## Caveats / Not Found

- **Claude Code TUI source is closed**: The public GitHub repo contains only plugins and examples. The main TUI rendering code is in a compiled binary with minified JS. Component names and UI strings were extracted via pattern matching on the minified bundle, which may miss some components or include false positives.
- **Codex CLI source is open but large**: The `codex-rs/tui/` directory has 150+ Rust source files. Analysis focused on the main structural modules; detailed rendering of every cell type was not exhaustively read.
- **No screenshots directly available**: Visual layout descriptions are inferred from code structure, documentation, and extracted UI strings rather than from actual screenshots.
- **Claude Code version analyzed**: v2.1.73 (from Trae extension) and v2.1.74 (darwin-x64). UI may differ in other versions.
- **Codex CLI version analyzed**: v0.139.0 (locally installed). Source from main branch of openai/codex repo.
- **Missing: animation timing details** - Both tools use animated indicators but the exact frame timing was not deeply analyzed.
- **Missing: theme/color scheme details for Claude Code** - Ink color names were found but the specific visual palette is not documented publicly.
