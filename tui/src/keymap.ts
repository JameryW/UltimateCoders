/**
 * TUI Keymap — centralized keyboard command definitions.
 *
 * Every shortcut, its applicable area, and display label is defined here.
 * App, CjkTextInput, StatusBar, and future help overlay all derive from
 * this single source of truth, ensuring "status bar says it, key does it."
 *
 * Convention:
 *   - Shift+Tab: cycle focus (input→chat→subtask→input)
 *   - Tab: indent in input; no-op elsewhere
 *   - Esc: from input→main pane; from main pane→input; closes detail
 *   - Ctrl+W: swap activeMainPane (chat↔subtask)
 *   - Ctrl+F: cycle event filter (any area)
 *   - Ctrl+P: pause/resume task (any area)
 *   - Ctrl+R: reconnect gRPC (any area)
 *   - Ctrl+L: clear chat log (chat focus)
 *   - Ctrl+Q: quit (any area)
 *   - PageUp/PageDown: scroll chat (chat focus)
 *   - Home/End: jump top/bottom of chat (chat focus)
 *   - Up/Down: scroll 1 line (chat) or navigate subtasks (subtask focus)
 *   - Enter: submit (input) or toggle detail (subtask)
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
  {id: 'cycleFocus', label: 'Cycle focus', shortLabel: 'S-Tab', key: 'Shift+Tab', areas: [], global: true},
  {id: 'escToMain', label: 'Escape to main / close detail', shortLabel: 'Esc', key: 'Esc', areas: [], global: true},
  {id: 'swapPane', label: 'Swap main pane', shortLabel: 'C-W', key: 'Ctrl+W', areas: [], global: true},
  {id: 'filter', label: 'Cycle event filter', shortLabel: 'C-F', key: 'Ctrl+F', areas: [], global: true},
  {id: 'pause', label: 'Pause/resume task', shortLabel: 'C-P', key: 'Ctrl+P', areas: [], global: true},
  {id: 'reconnect', label: 'Reconnect gRPC', shortLabel: 'C-R', key: 'Ctrl+R', areas: [], global: true},
  {id: 'quit', label: 'Quit', shortLabel: 'C-Q', key: 'Ctrl+Q', areas: [], global: true},
  // Note: ? is global except in input focus (where it types a character)
  {id: 'help', label: 'Show help', shortLabel: '?', key: '?', areas: [], global: true},

  // ── Input ──
  {id: 'indent', label: 'Insert indent', shortLabel: 'Tab', key: 'Tab', areas: ['input'], global: false},
  {id: 'newline', label: 'Insert newline', shortLabel: 'C-J', key: 'Ctrl+J', areas: ['input'], global: false},
  {id: 'submit', label: 'Submit task', shortLabel: 'Enter', key: 'Enter', areas: ['input'], global: false},
  {id: 'clearInput', label: 'Clear input', shortLabel: 'C-U', key: 'Ctrl+U', areas: ['input'], global: false},
  {id: 'deleteToEnd', label: 'Delete to end', shortLabel: 'C-K', key: 'Ctrl+K', areas: ['input'], global: false},
  {id: 'historyUp', label: 'History back', shortLabel: '↑', key: 'Up', areas: ['input'], global: false},
  {id: 'historyDown', label: 'History forward', shortLabel: '↓', key: 'Down', areas: ['input'], global: false},

  // ── Chat ──
  {id: 'scrollUp', label: 'Scroll up', shortLabel: '↑', key: 'Up', areas: ['chat'], global: false},
  {id: 'scrollDown', label: 'Scroll down', shortLabel: '↓', key: 'Down', areas: ['chat'], global: false},
  {id: 'pageUp', label: 'Page up', shortLabel: 'PgUp', key: 'PageUp', areas: ['chat'], global: false},
  {id: 'pageDown', label: 'Page down', shortLabel: 'PgDn', key: 'PageDown', areas: ['chat'], global: false},
  {id: 'home', label: 'Jump to top', shortLabel: 'Home', key: 'Home', areas: ['chat'], global: false},
  {id: 'end', label: 'Jump to bottom', shortLabel: 'End', key: 'End', areas: ['chat'], global: false},
  {id: 'expandAll', label: 'Expand/collapse long messages', shortLabel: 'Enter', key: 'Enter', areas: ['chat'], global: false},
  {id: 'clearLog', label: 'Clear log', shortLabel: 'C-L', key: 'Ctrl+L', areas: ['chat'], global: false},

  // ── Subtask ──
  {id: 'navUp', label: 'Previous subtask', shortLabel: '↑', key: 'Up', areas: ['subtask'], global: false},
  {id: 'navDown', label: 'Next subtask', shortLabel: '↓', key: 'Down', areas: ['subtask'], global: false},
  {id: 'navHome', label: 'First subtask', shortLabel: 'Home', key: 'Home', areas: ['subtask'], global: false},
  {id: 'navEnd', label: 'Last subtask', shortLabel: 'End', key: 'End', areas: ['subtask'], global: false},
  {id: 'jumpFailed', label: 'Jump to next failed', shortLabel: 'f', key: 'f', areas: ['subtask'], global: false},
  {id: 'toggleDetail', label: 'Toggle detail', shortLabel: 'Enter', key: 'Enter', areas: ['subtask'], global: false},
  {id: 'retrySubtask', label: 'Retry subtask (coming soon)', shortLabel: 'C-T', key: 'Ctrl+T', areas: ['subtask'], global: false},
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
  // Priority-ordered commands for status bar (most useful first)
  // Note: `area` is kept for future area-specific help; currently all areas show the same global shortcuts.
  const candidates: Array<{shortcut: string; label: string}> = [
    {shortcut: getCommand('cycleFocus')!.shortLabel, label: 'focus'},
    {shortcut: getCommand('help')!.shortLabel, label: 'help'},
    {shortcut: getCommand('reconnect')!.shortLabel, label: 'reconnect'},
    {shortcut: getCommand('quit')!.shortLabel, label: 'quit'},
  ];

  // Build text by adding shortcuts until we run out of budget
  // Budget: approximately terminalWidth / 4 chars for help (rest is for other segments)
  const helpBudget = Math.max(7, Math.floor(terminalWidth / 4));
  const parts: string[] = [];
  let usedWidth = 0;

  for (const cmd of candidates) {
    const entry = `${cmd.shortcut} ${cmd.label}`;
    const entryWidth = usedWidth > 0 ? 2 + entry.length : entry.length; // 2 spaces separator
    if (usedWidth + entryWidth <= helpBudget) {
      parts.push(entry);
      usedWidth += entryWidth;
    }
  }

  return parts.join('  ');
}
