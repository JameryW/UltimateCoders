/**
 * Slash command system — parse and dispatch /commands from TaskInput.
 *
 * Commands:
 *   /help       — show command list
 *   /tasks      — list tasks via gRPC
 *   /task <id>  — switch to a specific task
 *   /clear      — clear chat log
 *   /status     — show connection + active task info
 *   /reconnect  — reconnect gRPC
 *   /quit       — exit
 */

// ── Command Definitions ───────────────────────────────────────

export interface SlashCommand {
  /** Command name (without / prefix). */
  name: string;
  /** Short description for /help display. */
  description: string;
  /** Usage string (e.g. '/task <id>'). */
  usage: string;
  /** Whether this command takes arguments. */
  hasArgs: boolean;
}

export const COMMANDS: SlashCommand[] = [
  {name: 'help', description: 'Show command list', usage: '/help', hasArgs: false},
  {name: 'tasks', description: 'List all tasks', usage: '/tasks', hasArgs: false},
  {name: 'task', description: 'Switch to a task', usage: '/task <id>', hasArgs: true},
  {name: 'cancel', description: 'Cancel active task', usage: '/cancel', hasArgs: false},
  {name: 'clear', description: 'Clear chat log', usage: '/clear', hasArgs: false},
  {name: 'status', description: 'Show connection & task status', usage: '/status', hasArgs: false},
  {name: 'reconnect', description: 'Reconnect to gRPC server', usage: '/reconnect', hasArgs: false},
  {name: 'symbols', description: 'Set symbol mode (unicode/ascii/auto)', usage: '/symbols <mode>', hasArgs: true},
  {name: 'export', description: 'Export chat log to file', usage: '/export [path]', hasArgs: true},
  {name: 'quit', description: 'Exit the TUI', usage: '/quit', hasArgs: false},
];

/** Map command name → SlashCommand. */
const COMMAND_MAP = new Map(COMMANDS.map((c) => [c.name, c]));

// ── Parsing ───────────────────────────────────────────────────

export interface ParsedCommand {
  command: SlashCommand;
  args: string;
}

/**
 * Parse user input as a slash command.
 * Returns null if input doesn't start with / or command not found.
 */
export function parseCommand(input: string): ParsedCommand | null {
  const trimmed = input.trim();
  if (!trimmed.startsWith('/')) return null;

  const spaceIdx = trimmed.indexOf(' ');
  const name = spaceIdx < 0
    ? trimmed.slice(1).toLowerCase()
    : trimmed.slice(1, spaceIdx).toLowerCase();
  const args = spaceIdx < 0 ? '' : trimmed.slice(spaceIdx + 1).trim();

  const command = COMMAND_MAP.get(name);
  if (!command) return null;

  return {command, args};
}

/**
 * Get matching commands for autocomplete.
 * Returns commands whose name starts with the given prefix (without /).
 */
export function matchCommands(prefix: string): SlashCommand[] {
  const p = prefix.toLowerCase();
  if (!p) return COMMANDS;
  return COMMANDS.filter((c) => c.name.startsWith(p));
}

/**
 * Format command list for /help display.
 */
export function formatHelpText(): string {
  const lines = COMMANDS.map((c) => `  ${c.usage.padEnd(16)} ${c.description}`);
  return `Commands:\n${lines.join('\n')}`;
}

/**
 * Check if input starts with / (potential command).
 */
export function isCommandInput(input: string): boolean {
  return input.trimStart().startsWith('/');
}
