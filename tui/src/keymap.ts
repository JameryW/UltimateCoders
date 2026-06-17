/**
 * TUI Keymap — centralized keyboard command definitions.
 *
 * Every shortcut, its applicable area, and display label is defined here.
 * App, CjkTextInput, StatusBar, and help overlay all derive from
 * this single source of truth, ensuring "status bar says it, key does it."
 *
 * Convention (v3 — single-column vertical):
 *   - Shift+Tab / Ctrl+W: cycle focus (input→chat→input)
 *   - Tab: indent in input; no-op elsewhere
 *   - Esc: from input→chat; from chat→input; closes overlays
 *   - Ctrl+T: toggle subtask overlay (global)
 *   - Ctrl+F: cycle event filter (global)
 *   - Ctrl+P: pause/resume task (global)
 *   - Ctrl+R: reconnect gRPC (global)
 *   - Ctrl+L: clear chat log (chat focus)
 *   - Ctrl+Q: quit (global)
 *   - PageUp/PageDown: scroll chat by page (chat focus)
 *   - Home/End: jump top/bottom of chat (chat focus); cursor start/end (input)
 *   - Ctrl+Left/Right: word navigation (input)
 *   - Up/Down: scroll 1 line (chat focus)
 *   - Enter: submit (input) or expand/collapse (chat focus)
 */

import type {FocusedArea} from './reducer.js';

// ── Command Definitions ────────────────────────────────────

export interface KeyCommand {
  /** Unique command identifier. */
  id: string;
  /** Human-readable label for status bar and help. */
  label: string;
  /** Short status bar hint (fits in ~8 chars). */
  shortLabel: string;
  /** Key combination display string. */
  key: string;
  /** Areas where this command is active. Empty = global. */
  areas: FocusedArea[];
  /** Whether this command is active in ALL areas (overrides areas). */
  global: boolean;
}

const COMMANDS: KeyCommand[] = [
  // ── Global ──
  {id: 'cycleFocus', label: 'Cycle focus', shortLabel: 'S-Tab', key: 'Shift+Tab / Ctrl+W', areas: [], global: true},
  {id: 'escToMain', label: 'Escape / close overlay', shortLabel: 'Esc', key: 'Esc', areas: [], global: true},
  {id: 'subtaskOverlay', label: 'Toggle subtask overlay', shortLabel: 'C-T', key: 'Ctrl+T', areas: [], global: true},
  {id: 'filter', label: 'Cycle event filter', shortLabel: 'C-F', key: 'Ctrl+F', areas: [], global: true},
  {id: 'pause', label: 'Pause/resume task', shortLabel: 'C-P', key: 'Ctrl+P', areas: [], global: true},
  {id: 'reconnect', label: 'Reconnect gRPC', shortLabel: 'C-R', key: 'Ctrl+R', areas: [], global: true},
  {id: 'quit', label: 'Quit', shortLabel: 'C-Q', key: 'Ctrl+Q', areas: [], global: true},
  // Note: ? is global except in input focus (where it types a character)
  {id: 'help', label: 'Show help', shortLabel: '?', key: '?', areas: [], global: true},

  // ── Input ──
  {id: 'indent', label: 'Insert indent', shortLabel: 'Tab', key: 'Tab', areas: ['input'], global: false},
  {id: 'newline', label: 'Insert newline', shortLabel: 'C-J', key: 'Ctrl+J / Alt+Enter', areas: ['input'], global: false},
  {id: 'submit', label: 'Submit task', shortLabel: 'Enter', key: 'Enter', areas: ['input'], global: false},
  {id: 'clearInput', label: 'Clear input', shortLabel: 'C-U', key: 'Ctrl+U', areas: ['input'], global: false},
  {id: 'deleteToEnd', label: 'Delete to end', shortLabel: 'C-K', key: 'Ctrl+K', areas: ['input'], global: false},
  {id: 'historyUp', label: 'History back', shortLabel: '↑', key: 'Up', areas: ['input'], global: false},
  {id: 'historyDown', label: 'History forward', shortLabel: '↓', key: 'Down', areas: ['input'], global: false},
  {id: 'wordBack', label: 'Word backward', shortLabel: 'C-←', key: 'Ctrl+Left', areas: ['input'], global: false},
  {id: 'wordForward', label: 'Word forward', shortLabel: 'C-→', key: 'Ctrl+Right', areas: ['input'], global: false},
  {id: 'inputHome', label: 'Cursor to start', shortLabel: 'Home', key: 'Home', areas: ['input'], global: false},
  {id: 'inputEnd', label: 'Cursor to end', shortLabel: 'End', key: 'End', areas: ['input'], global: false},
  {id: 'undo', label: 'Undo', shortLabel: 'M-Z', key: 'Cmd+Z / Alt+Z', areas: ['input'], global: false},
  {id: 'redo', label: 'Redo', shortLabel: 'M-S-Z', key: 'Cmd+Shift+Z / Alt+Shift+Z', areas: ['input'], global: false},
  {id: 'paste', label: 'Paste text', shortLabel: 'Paste', key: 'Ctrl+V / Cmd+V', areas: ['input'], global: false},

  // ── Chat ──
  {id: 'scrollUp', label: 'Scroll up', shortLabel: '↑', key: 'Up', areas: ['chat'], global: false},
  {id: 'scrollDown', label: 'Scroll down', shortLabel: '↓', key: 'Down', areas: ['chat'], global: false},
  {id: 'pageUp', label: 'Page up', shortLabel: 'PgUp', key: 'PageUp', areas: ['chat'], global: false},
  {id: 'pageDown', label: 'Page down', shortLabel: 'PgDn', key: 'PageDown', areas: ['chat'], global: false},
  {id: 'home', label: 'Jump to top', shortLabel: 'Home', key: 'Home', areas: ['chat'], global: false},
  {id: 'end', label: 'Jump to bottom', shortLabel: 'End', key: 'End', areas: ['chat'], global: false},
  {id: 'expandAll', label: 'Expand/collapse long messages', shortLabel: 'Enter', key: 'Enter', areas: ['chat'], global: false},
  {id: 'clearLog', label: 'Clear log', shortLabel: 'C-L', key: 'Ctrl+L', areas: ['chat'], global: false},

  // ── Overlay (subtask Ctrl+T) ──
  {id: 'overlayUp', label: 'Navigate subtask up', shortLabel: '↑', key: 'Up', areas: [], global: false},
  {id: 'overlayDown', label: 'Navigate subtask down', shortLabel: '↓', key: 'Down', areas: [], global: false},
  {id: 'overlayDetail', label: 'Toggle subtask detail', shortLabel: 'Enter', key: 'Enter', areas: [], global: false},
  {id: 'overlayRetry', label: 'Retry failed subtask', shortLabel: 'R', key: 'R', areas: [], global: false},
];

// ── Lookup Helpers ─────────────────────────────────────────

/** All commands indexed by id. */
const byId = new Map(COMMANDS.map((c) => [c.id, c]));

/** Get a command by id. */
export function getCommand(id: string): KeyCommand | undefined {
  return byId.get(id);
}

/** Get all commands for a given focus area (including global). */
export function getCommandsForArea(area: FocusedArea): KeyCommand[] {
  return COMMANDS.filter((c) => c.global || c.areas.includes(area));
}

// ── Status Bar Text ────────────────────────────────────────

/**
 * Generate the status bar help text for a given focus area and terminal width.
 * Returns a compact string like: "S-Tab focus  ? help"
 *
 * Budget-based: only outputs the most useful 2-3 shortcuts that fit.
 * Full shortcuts are in the ? help overlay.
 */
export function getStatusBarHelp(area: FocusedArea, terminalWidth: number): string {
  const candidates: Array<{shortcut: string; label: string}> = [
    {shortcut: getCommand('cycleFocus')!.shortLabel, label: 'focus'},
    {shortcut: getCommand('subtaskOverlay')!.shortLabel, label: 'subtasks'},
    {shortcut: getCommand('help')!.shortLabel, label: 'help'},
    {shortcut: getCommand('quit')!.shortLabel, label: 'quit'},
  ];

  const helpBudget = Math.max(7, Math.floor(terminalWidth / 4));
  const parts: string[] = [];
  let usedWidth = 0;

  for (const cmd of candidates) {
    const entry = `${cmd.shortcut} ${cmd.label}`;
    const entryWidth = usedWidth > 0 ? 2 + entry.length : entry.length;
    if (usedWidth + entryWidth <= helpBudget) {
      parts.push(entry);
      usedWidth += entryWidth;
    }
  }

  return parts.join('  ');
}
