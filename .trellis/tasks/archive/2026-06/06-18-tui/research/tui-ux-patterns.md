# Research: TUI UX Patterns (2025-2026)

- **Query**: Modern terminal UI best practices and UX patterns used by popular CLI tools
- **Scope**: Mixed (internal codebase audit + external tool/library research)
- **Date**: 2026-06-18

## Findings

---

## 1. Mouse Support in Terminal Apps

### What Popular Tools Do

**lazygit** (Go / tcell):
- Fully mouse-driven: click to select items, scroll wheel to navigate lists, click to toggle panels
- Enables mouse on startup via terminal mouse protocol (SGR-1006 preferred, falls back to X10/urxvt)
- Uses Go's `tcell` library which abstracts mouse protocols automatically

**Helix** (Rust / crossterm):
- Mouse support for cursor positioning, text selection, and scrolling
- Uses crossterm which handles SGR mouse protocol
- Can be disabled in config (`[editor] mouse = false`)

**Claude Code** (Node.js / Ink-based):
- Limited mouse support: scroll wheel works for chat history navigation
- Click to position cursor in input field
- Uses Ink's raw stdin access (not a built-in useMouse hook)
- Mouse events parsed from terminal escape sequences manually

**Aider** (Python / prompt_toolkit):
- Full mouse support via prompt_toolkit: click, scroll, selection
- prompt_toolkit handles all mouse protocols transparently

### Terminal Mouse Protocols

| Protocol | Sequence | Support |
|----------|----------|---------|
| X10 (basic) | `ESC[M` + 3 bytes | Oldest, most compatible, button press only |
| X11 (normal) | `ESC[M` + 3 bytes with modifiers | Supports button release, motion |
| SGR-1006 | `ESC[<` + semicolon-separated params | Modern, reliable coordinate encoding, wide terminal support |
| urxvt | `ESC[` + number + semicolons + `M/m` | Used by rxvt-unicode |

To enable mouse: write `\x1b[?1000h` (basic), `\x1b[?1002h` (button motion), or `\x1b[?1006h` (SGR) to stdout. Disable with `\x1b[?1000l`.

### Libraries/Approaches for Node.js/Ink

| Library | Mouse Support | Notes |
|---------|--------------|-------|
| **Ink v5** | None built-in | No `useMouse` hook; must parse mouse escape sequences from raw stdin |
| **Ink v6.6** | None built-in | Same as v5 -- no mouse hook |
| **Ink v7.1** | None built-in | Still no `useMouse`; has `usePaste`, `useAnimation`, `useCursor`, `useWindowSize`, `useBoxMetrics`, kitty keyboard protocol |
| **terminal-kit** v3 | Full mouse (GPM + X10/SGR) | Complete mouse event system with click, scroll, drag; NOT React-based |
| **blessed** v0.1 | Full mouse | Curses-like TUI with full mouse support; NOT React-based |
| **@clack/prompts** v1.5 | No mouse | Keyboard-only interactive prompts |

### What's Realistic for Ink v5

- **Scroll wheel**: Parse SGR mouse sequences from `useStdin().internal_eventEmitter`. When chat area is focused, translate scroll-up/scroll-down to `SCROLL_UP`/`SCROLL_DOWN` dispatches. This is the highest-value mouse feature.
- **Click to focus**: Detect mouse click position, determine if it falls in input area or chat area, dispatch `CYCLE_FOCUS`. Requires knowing component Y positions.
- **Click to select message**: Requires coordinate mapping from mouse Y to ChatLog visible line index. Complex due to variable-height messages.
- **Implementation approach**: Write a custom `useMouse` hook that:
  1. Enables SGR mouse mode (`\x1b[?1006h`) on mount
  2. Listens on `internal_eventEmitter` for `\x1b[<` sequences
  3. Parses button, modifiers, row, col from the SGR format
  4. Disables mouse mode (`\x1b[?1006l`) on unmount
- **Caveat**: Ink's render cycle may interfere with mouse positioning. Must test that mouse enable/disable sequences don't corrupt Ink's output.

---

## 2. Search/Highlight in Terminal Chat Logs

### What Popular Tools Do

**lazygit**:
- Search via `/` key in any panel (files, commits, branches)
- Incremental search: highlights matches as you type
- `n`/`N` to jump to next/previous match
- Uses tcell's input handling for search bar at bottom of panel

**WeeChat** (C/ncurses):
- `Ctrl+R` opens buffer search
- Search bar appears at bottom; type query, matches highlight in buffer
- Regex support; case-insensitive by default
- `Enter` to jump to match, `Up/Down` to cycle through matches

**less** (Unix pager):
- `/` for forward search, `?` for backward search
- Pattern highlighting with standout mode
- `n`/`N` for next/previous match

**Claude Code**:
- No explicit search in chat history
- Relies on scrolling (PageUp/PageDown) to find messages
- Uses `/` for slash commands, so cannot use standard search binding

### Pattern for Searchable Message History

1. **Search mode**: Enter via a keybinding (e.g., `Ctrl+S` or `/` in chat focus). Shows a search input bar.
2. **Incremental search**: As user types, filter/highlight matching messages in the visible window.
3. **Match navigation**: `n`/`N` or `Enter`/`Shift+Enter` to jump between matches.
4. **Highlight**: Use ANSI inverse (`\x1b[7m`) or colored background to highlight matched text within messages.
5. **Exit**: `Esc` to close search bar and clear highlights.

### Libraries/Approaches for Node.js/Ink

| Approach | Description |
|----------|-------------|
| **Custom search state in reducer** | Add `searchQuery`, `searchMatches` (array of message IDs + line offsets), `searchMatchIndex` to TuiState |
| **Regex-based filtering** | Use `new RegExp(query, 'i')` against message text; collect matching message IDs |
| **In-text highlighting** | Split message text by regex match boundaries; render matched segments with `<Text backgroundColor="yellow">` or ANSI inverse |
| **Search input component** | Reuse `CjkTextInput` component in a search bar below ChatLog's indicator bar |

### What's Realistic for Ink v5

- **Message-level search** (filter to matching messages): Straightforward. Add a search mode that filters `filteredMessages` further by text content. Similar to existing `eventFilter`.
- **In-text highlighting**: Moderate complexity. Must split rendered text at match boundaries and wrap matched segments in `<Text>` with different styling. Works with Ink's `<Text>` component but requires restructuring `ChatMessageItem` rendering.
- **Search bar UI**: Use a dedicated search bar (similar to TaskInput) that appears above the input area when search mode is active. Can reuse CjkTextInput.
- **Suggested keybinding**: `Ctrl+S` (not used currently). `/` conflicts with slash commands but could be used when `focusedArea === 'chat'`.

---

## 3. Code Rendering in Terminal

### What Popular Tools Do

**delta** (Rust):
- Side-by-side diff rendering with syntax highlighting
- Line numbers, change indicators (+/-), file headers
- Uses bat's syntax highlighting engine (syntect)
- Configurable themes, handles wide characters
- The gold standard for terminal diff rendering

**Aider** (Python / Pygments):
- Syntax-highlighted code blocks in chat
- Uses Pygments for syntax highlighting
- Diff rendering with `+`/`-` prefixes and color coding

**Claude Code**:
- Renders code blocks with syntax highlighting in chat messages
- Uses a custom markdown renderer that applies chalk colors to code spans
- Diff output shown with colored additions (green) and deletions (red)

**lazygit**:
- Shows diff with `+`/`-` color coding (green/red)
- No syntax highlighting within diff lines
- Simple and fast

### Syntax Highlighting Libraries for Node.js Terminal

| Library | Version | Description | Terminal Output |
|---------|---------|-------------|----------------|
| **cli-highlight** | 2.1.11 | highlight.js wrapper for terminal | ANSI colored strings |
| **highlight.js** | 11.x | The engine behind cli-highlight | HTML by default; cli-highlight converts to ANSI |
| **shiki** | 4.2.0 | VS Code's textmate grammar engine | HTML output; needs ANSI conversion for terminal |
| **marked-terminal** | 7.3.0 | Markdown-to-terminal renderer | Uses cli-highlight internally for code blocks |
| **ink-stream-markdown** | 0.0.4 | Streaming markdown for Ink | Uses shiki; Ink-compatible React components |

**Already in use by this project**: `marked-terminal@7.3.0` + `cli-highlight@2.1.11` (as transitive dependency) for markdown rendering in ChatLog.

### Diff Rendering Approaches

| Approach | Description | Complexity |
|----------|-------------|------------|
| **Unified diff with color** | Show `+` lines green, `-` lines red, context lines normal | Low |
| **Inline diff highlighting** | Within changed lines, highlight the specific changed characters | Medium |
| **Side-by-side diff** | Two columns showing old/new | High (needs wide terminal, complex layout) |
| **Delta-style** | Full featured: syntax highlight + diff + line numbers | Very High |

### What's Realistic for Ink v5

- **Syntax highlighting in code blocks**: Already works via `marked-terminal` + `cli-highlight`. The current `renderMarkdown()` function in `markdown.ts` handles this. However, `cli-highlight` v2.1.11 uses highlight.js v9.x which is outdated; many newer languages/grammars are missing.
- **Upgrading code highlighting**: Two paths:
  1. Upgrade to `cli-highlight` with newer highlight.js (if available) -- simplest
  2. Switch to `shiki` for better grammar coverage -- requires ANSI output conversion, more complex but much better results. `ink-stream-markdown` already wraps this.
- **Diff rendering**: Start with unified diff + color coding (green additions, red deletions). This is achievable by:
  1. Detecting diff content in messages (lines starting with `+`/`-`/`@@`)
  2. Rendering with `<Text color="green">` / `<Text color="red">` respectively
  3. Adding `diff` as a recognized language in the code highlighter
- **Side-by-side diff**: Not practical in current single-column layout. Would require a dedicated overlay or horizontal split.
- **Language detection for code blocks**: `cli-highlight` auto-detects language. Could also pass `file_modified` event's filename extension to hint the language.

---

## 4. Task/Progress Visualization

### What Popular Tools Do

**htop** (C/ncurses):
- Per-process progress bars with percentage
- Tree view with indentation for parent-child relationships
- Real-time updates via timer-based refresh
- Color-coded status (running=green, sleeping=blue, etc.)

**lazygit**:
- Simple progress bars for git operations (push/pull)
- Spinner animation for loading states
- Status text for operation results

**Claude Code**:
- Spinner animation during task execution
- Token usage counter
- Simple "Working..." / "Done" status text
- No subtask visualization

**Aider**:
- Per-file change indicators (added/modified/deleted)
- Token cost tracking
- Simple status line

**GitHub CLI (gh)**:
- Spinner with task description
- Progress dots for long operations
- Checkmark/cross for completion status

### Multi-Task Progress Patterns

| Pattern | Description | Example |
|---------|-------------|---------|
| **Inline progress bar** | `▓▓▓░░░ 50%` within text | Current TUI StatusIndicator |
| **Per-item status icons** | Each item has status icon + text | Current TUI SubtaskTree |
| **Tree progress** | Indented tree with per-node status | htop process tree |
| **Kanban columns** | Columns for status categories | Not practical in terminal |
| **Timeline/gantt** | Horizontal bars for duration | Not common in terminal |
| **Sparkline** | Compact character-based chart | For metrics over time |

### What's Realistic for Ink v5

- **Current state**: The TUI already has:
  - `StatusIndicator`: spinner + elapsed + `[▓▓░░] 2/5` progress bar
  - `SubtaskTree`: per-subtask status icons with progress bar
  - Inline subtask summary: `2/5 | 1 in_progress | 0 failed`
- **Improvements that are realistic**:
  1. **Elapsed time per subtask**: Show duration next to each in-progress/completed subtask (e.g., `◉ 2. Read file (12s)`)
  2. **Dependency chain visualization**: Show which subtasks are blocked by pending dependencies using tree indentation
  3. **Compact multi-task view**: When multiple tasks exist, show a summary row per task with status icon + progress bar
  4. **Animated progress**: Ink v5 lacks `useAnimation` (available in Ink v7); current workaround uses `setInterval` at 80ms for spinner frames
- **Kanban-style view**: Not practical in terminal. The overlay model (Ctrl+T) with per-subtask status is the closest terminal equivalent.
- **ink-progress-bar** (v3.0.0): Exists as an Ink component but adds a dependency for a feature that is easy to implement with a few lines of chalk/Text.

---

## 5. Configuration UI

### What Popular Tools Do

**lazygit**:
- Config file only: `~/.config/lazygit/config.yml`
- Extensive configuration (keybindings, colors, git behavior, UI layout)
- No inline settings UI; users must edit config file and restart
- Config docs: https://github.com/jesseduffield/lazygit/blob/master/docs/Config.md

**Helix**:
- Config file only: `~/.config/helix/config.toml`
- Theme, keybindings, editor settings, lsp config
- No inline settings UI
- Config auto-reloads on save

**Aider**:
- Config file: `.aider.conf.yml` in project root or home directory
- Also supports command-line flags and env vars
- `/config` slash command shows current settings
- No interactive config editor

**Claude Code**:
- Config file: `~/.claude/settings.json` + project-level `.claude/settings.json`
- `/config` command to view/change settings
- Settings include: model, theme, permissions, allowed tools
- Layered config: user > project > session

**Git**:
- `git config` command with `--global`, `--local`, `--system` scopes
- `git config --edit` opens config in $EDITOR
- `git config <key> <value>` for individual settings

### Common Pattern: Config File + Slash Command

Most terminal tools use:
1. **Config file** for persistent settings (YAML, TOML, or JSON)
2. **Slash command** (`/config`, `/settings`) to view current config or set individual values
3. **Env vars** as overrides for CI/container environments
4. **No inline GUI** for editing settings (terminal space is too limited)

### Libraries for Node.js Config Management

| Library | Version | Description |
|---------|---------|-------------|
| **conf** | 15.1.0 | XDG-compliant config with JSON schema validation, atomic writes |
| **rc** | 1.2.8 | Hierarchical config: defaults + env + argv + config files |
| **cosmiconfig** | 5.x | Searches for config in package.json, .rc files, etc. |
| **dotenv** | 16.x | .env file parsing for env vars |

### What's Realistic for Ink v5

- **Config file approach**: Use `conf` (v15) for XDG-compliant JSON config at `~/.config/ultimate-coders/tui.json`. This handles:
  - gRPC server address (default: `localhost:50051`)
  - Symbol mode (unicode/ascii/auto)
  - Message cap (default: 2000)
  - Default event filter
  - Input history size (default: 50)
  - Notification preferences (bell/OSC 777/desktop)
- **Slash command**: Add `/config` command that shows current settings or sets individual values:
  - `/config` -- show all settings
  - `/config server localhost:50051` -- set gRPC server address
  - `/config symbols unicode` -- force symbol mode
- **Env var overrides**: `UC_GRPC_SERVER`, `UC_SYMBOL_MODE`, `UC_NO_COLOR` (already supports `NO_COLOR`)
- **No inline settings panel**: Terminal space is too limited for an interactive settings UI. The slash command approach is standard.
- **Config persistence**: `conf` auto-creates config directory and file. Settings persist across TUI sessions.

---

## 6. Theme/Color Schemes

### What Popular Tools Do

**lazygit**:
- User-defined themes in config.yml
- Preset themes: default, dark, light, solarized, catppuccin, dracula, nord, etc.
- Custom color mapping for every UI element
- Theme gallery: https://github.com/jesseduffield/lazygit/wiki/Themes

**Helix**:
- Built-in themes (26+): dracula, nord, catppuccin, tokyo_night, etc.
- Theme files in TOML format
- Custom themes via `~/.config/helix/themes/`
- Theme sets: `ui`, `syntax`, `diagnostic` scopes
- Theme inherits from base theme

**bat** (Rust):
- Uses syntect for syntax highlighting themes
- 20+ built-in themes (based on TextMate/Sublime themes)
- `bat --list-themes` to preview
- Theme config in `~/.config/bat/config`

**Delta** (Rust):
- Inherits bat's theme system for syntax highlighting
- Separate diff coloring config
- Light/dark mode detection via terminal color scheme queries (OSC 11)

### Dark/Light Detection in Terminal

| Method | Description | Reliability |
|--------|-------------|-------------|
| **OSC 11 query** | `ESC]11;?\x1b\\` asks terminal for background color; parse RGB to determine light/dark | Good on modern terminals (iTerm2, Kitty, WezTerm, Alacritty). Not supported by screen/tmux |
| **COLORFGBG env var** | Set by some terminals (xterm, rxvt) | Limited support |
| **TERM_PROGRAM** | Identify terminal app, assume its default theme | Fragile |
| **NO_COLOR** | Standard env var to disable colors entirely | Good (already respected by symbols.ts) |
| **COLORTERM** | `truecolor` or `24bit` indicates 16M color support | Good for feature detection |
| **User config** | Explicit setting in config file | Most reliable |

### Libraries/Approaches for Node.js/Ink

| Approach | Description |
|----------|-------------|
| **Chalk** (already installed) | Automatic color support detection; respects `NO_COLOR`, `FORCE_COLOR` |
| **supports-color** | Detects terminal color capabilities (256, 16M, etc.) |
| **Custom theme system** | Define color tokens in a theme object; map to Chalk/Ink `<Text color>` |
| **OSC 11 probe** | Write `\x1b]11;?\x1b\\` to stdout, read response from stdin to get background color |

### What's Realistic for Ink v5

- **Current state**: The TUI uses hardcoded color names in `<Text color="...">` and `chalk.color()`. `symbols.ts` has unicode/ascii/auto detection. `NO_COLOR` is respected.
- **Realistic improvements**:
  1. **Theme object**: Create a `Theme` interface with semantic color tokens (e.g., `success`, `error`, `warning`, `info`, `muted`, `accent`, `primary`). Map these to actual colors based on a theme selection.
  2. **2-3 built-in themes**: "default" (current colors), "high-contrast" (brighter colors for readability), "monochrome" (no colors, only bold/dim).
  3. **OSC 11 probe**: On startup, query terminal background color. If background is dark (RGB avg < 128), use default theme; if light, use high-contrast or light theme. Fallback to config setting.
  4. **Config file theme setting**: `theme: "auto" | "dark" | "light" | "high-contrast" | "mono"` in `~/.config/ultimate-coders/tui.json`.
  5. **Slash command**: `/theme` to show current theme, `/theme <name>` to switch.
- **Not realistic**: Full custom color mapping like lazygit (too many color tokens for our UI size); Sublime/TextMate-compatible syntax themes (overkill for terminal code rendering).

---

## 7. Log Export

### What Popular Tools Do

**lazygit**:
- No built-in export; users pipe git commands for export
- Can copy commit hashes, file paths to clipboard via `c` key

**htop**:
- No export; screen-oriented tool
- Output goes to terminal only

**Aider**:
- `.aider.chat.history.md` -- auto-saves chat history as markdown
- `/diff` command outputs current changes
- `/history` shows session history

**Claude Code**:
- `--output-format json` for machine-readable output
- Conversation history saved in `~/.claude/` directory
- Can copy messages to clipboard

**Git**:
- `git log > output.txt` -- pipe-friendly
- `git format-patch` -- structured export
- `git diff > changes.patch` -- patch export

### Common Log Export Patterns

| Pattern | Description | Use Case |
|---------|-------------|----------|
| **Write to file** | Append session log to `~/.local/share/app/sessions/` | Archival, audit trail |
| **Copy to clipboard** | OSC 52 escape or `clipboardy` npm package | Quick sharing |
| **Pipe-friendly output** | `--json` flag or structured stdout | Integration with other tools |
| **Markdown export** | Render messages as `.md` file | Human-readable reports |
| **JSONL export** | One JSON object per line per message | Machine parsing |

### Clipboard Access from Terminal

| Method | Description | Cross-platform |
|--------|-------------|----------------|
| **OSC 52** | `ESC]52;c;BASE64\x1b\\` escape sequence | Works in tmux, screen, iTerm2, Kitty. NOT in most Linux terminal emulators without config |
| **clipboardy** (v5.3.1) | Native clipboard access via pbcopy/xclip/wl-copy | Good cross-platform support |
| **child_process** | `spawn('pbcopy')` (macOS) / `spawn('xclip', ['-selection', 'clipboard'])` (Linux) | Manual but works |

### What's Realistic for Ink v5

- **Session log file**: On TUI startup, create a timestamped log file at `~/.local/share/ultimate-coders/sessions/YYYY-MM-DD-HHMMSS.log`. Append each ChatMessage as it arrives (plain text with timestamps). This provides an audit trail without any UI.
- **Slash command for export**:
  - `/export` -- write full session to `~/.local/share/ultimate-coders/sessions/` as markdown
  - `/export --json` -- JSONL format (one JSON object per message)
  - `/copy <message-id>` -- copy selected message text to clipboard via OSC 52
- **Clipboard copy**: Use OSC 52 escape sequence as primary method (works in tmux/screen). Fallback to `clipboardy` for environments where OSC 52 is not supported. The TUI already writes raw escape sequences for cursor control; adding OSC 52 is consistent.
- **Config option**: `logDir` in config file to customize where session logs are stored. Default follows XDG spec.
- **Not realistic**: Inline copy-to-clipboard for arbitrary text selections (requires mouse support + text selection tracking). Instead, focus on copying full messages or selected content.

---

## Summary: Ink v5 Capabilities and Limitations

### Ink v5 vs v7 Feature Comparison

| Feature | Ink v5 (current) | Ink v7 (latest) | Migration Effort |
|---------|-------------------|------------------|------------------|
| React | 18 | 19 | Breaking change in React 19 |
| useInput Key type | No home/end/super/hyper | Has home, end, super, hyper, eventType (with kitty protocol) | Medium (removes ponytail workarounds) |
| useAnimation | No (use setInterval workaround) | Yes | Low (already implemented via workaround) |
| useCursor | Custom hook (hide only) | Built-in | Low |
| usePaste | No | Yes (bracketed paste) | Low |
| useWindowSize | No (use useStdout) | Yes | Low |
| useBoxMetrics | No | Yes | New capability |
| useMouse | No | No | N/A -- must implement manually in both versions |
| Kitty keyboard | No | Yes | New capability |

### Ink v5 Upgrade Path

- **Ink v6/v7 require React 19**, which is a breaking change for the entire project. The project currently uses React 18.3.1.
- Multiple `ponytail` markers in the codebase note "remove when upgrading to Ink v6.6+"
- Ink v7 is the current latest but requires React 19 migration first
- Until React 19 migration, all new capabilities must be built on Ink v5

### Priority Features by Implementation Difficulty

| Feature | Difficulty | Impact | Approach |
|---------|-----------|--------|----------|
| Search/highlight in ChatLog | Medium | High | Add search state to reducer, search bar using CjkTextInput, regex filter + ANSI inverse highlighting |
| Config file + /config command | Low | High | Use `conf` npm package for XDG-compliant JSON config |
| Log export (session file) | Low | Medium | Append messages to timestamped file on disk |
| Clipboard via OSC 52 | Low | Medium | Write OSC 52 escape sequence on /copy command |
| Scroll wheel (mouse) | Medium | Medium | Custom useMouse hook parsing SGR sequences from raw stdin |
| Theme/color system | Medium | Medium | Theme object with semantic tokens + OSC 11 probe for dark/light |
| Diff rendering with color | Low | Medium | Detect diff lines, render + in green / - in red |
| Click-to-focus (mouse) | Medium-High | Low-Medium | Requires Y-coordinate mapping to UI areas |
| Syntax highlighting upgrade (shiki) | High | Medium | Replace cli-highlight with shiki; requires ANSI conversion |
| Side-by-side diff | Very High | Low | Not practical in single-column layout |

---

## Related Specs

- `.trellis/spec/frontend/tui-grpc-spec.md` -- Complete TUI component contracts, keyboard model, state management
- `.trellis/tasks/06-18-tui/research/tui-audit-findings.md` -- Internal codebase audit (P0/P1/P2 issues)

## Caveats / Not Found

- **No `useMouse` hook in any Ink version** (v5 through v7.1). Mouse support must be built from scratch using raw stdin escape sequence parsing.
- **Ink v7.1** was the latest version checked. Ink may add mouse support in future releases but there are no open issues/PRs suggesting this is planned.
- **Claude Code's** specific mouse implementation details are not publicly documented; findings are based on observable behavior.
- **ink-stream-markdown** (v0.0.4) is very early-stage (0.x version) and may not be production-ready despite using shiki.
- **cli-highlight** v2.1.11 bundles highlight.js v9.x which is significantly outdated; the latest highlight.js is v11.x with many more language grammars. However, cli-highlight has not been updated to use a newer highlight.js.
- No research was conducted on **bracketed paste** support patterns (noted as a TODO in CjkTextInput.tsx:170 but not in scope for this research).
