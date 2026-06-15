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
 * Returns a compact string like: "S-Tab focus  C-W swap  C-P pause  C-R reconnect  C-Q quit"
 */
export function getStatusBarHelp(area: FocusedArea, terminalWidth: number): string {
  const isNarrow = terminalWidth < 80;
  const isMedium = terminalWidth >= 80 && terminalWidth < 100;

  // Priority-ordered commands to show in status bar
  const globalCmds = [
    getCommand('cycleFocus')!,
    getCommand('swapPane')!,
    getCommand('filter')!,
    getCommand('pause')!,
    getCommand('reconnect')!,
    getCommand('quit')!,
  ];

  const areaCmds = getCommandsForArea(area).filter((c) => !c.global);

  if (isNarrow) {
    // Minimal: just cycle focus + quit
    return `${getCommand('cycleFocus')!.shortLabel} focus  ${getCommand('quit')!.shortLabel} quit`;
  }

  if (isMedium) {
    // Medium: global shortcuts only
    return globalCmds.map((c) => `${c.shortLabel} ${c.label.toLowerCase().split(' ')[0]}`).join('  ');
  }

  // Wide: global + area-specific
  const parts = globalCmds.map((c) => `${c.shortLabel} ${c.label.toLowerCase().split(' ')[0]}`);
  if (areaCmds.length > 0) {
    // Show first 2 area-specific commands
    const areaPart = areaCmds.slice(0, 2).map((c) => `${c.shortLabel} ${c.label.toLowerCase().split(' ')[0]}`);
    parts.push(...areaPart);
  }
  return parts.join('  ');
}
