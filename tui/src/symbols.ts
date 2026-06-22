/**
 * Symbol strategy — terminal-safe character rendering.
 *
 * Three modes:
 * - unicode: full Unicode symbols (circles, arrows, box lines)
 * - ascii: ASCII-safe fallback ([ ], [x], [~], etc.)
 * - auto: detect based on TERM / NO_COLOR / CI environment
 *
 * Usage:
 *   const S = getSymbols(); // auto-detect
 *   S.pending  // '○' or '[ ]'
 *   S.divider  // '─' or '-'
 */

export type SymbolMode = 'unicode' | 'ascii' | 'auto';

// ── Symbol Sets ─────────────────────────────────────────────

export interface SymbolSet {
  // Brand
  brand: string;

  // Subtask status icons
  pending: string;
  assigned: string;
  inProgress: string;
  completed: string;
  failed: string;
  conflicted: string;

  // Connection indicators
  connected: string;
  connecting: string;
  disconnected: string;
  error: string;

  // Layout
  divider: string;
  verticalSep: string;

  // Arrows
  arrowRight: string;
  arrowDown: string;
  arrowUp: string;

  // Misc
  check: string;
  cross: string;
  bullet: string;

  // Progress bar
  barFilled: string;
  barHalf: string;
  barEmpty: string;
}

const UNICODE: SymbolSet = {
  brand: '◆',
  pending: '○',
  assigned: '◌',
  inProgress: '◉',
  completed: '●',
  failed: '✗',
  conflicted: '⚠',
  connected: '●',
  connecting: '◌',
  disconnected: '○',
  error: '✗',
  divider: '─',
  verticalSep: '│',
  arrowRight: '→',
  arrowDown: '↓',
  arrowUp: '↑',
  check: '✓',
  cross: '✗',
  bullet: '•',
  barFilled: '▓',
  barHalf: '▒',
  barEmpty: '░',
};

const ASCII: SymbolSet = {
  brand: '*',
  pending: '[ ]',
  assigned: '[~]',
  inProgress: '[*]',
  completed: '[x]',
  failed: '[!]',
  conflicted: '[?]',
  connected: '*',
  connecting: '~',
  disconnected: 'o',
  error: '!',
  divider: '-',
  verticalSep: '|',
  arrowRight: '->',
  arrowDown: 'v',
  arrowUp: '^',
  check: 'ok',
  cross: 'X',
  bullet: '*',
  barFilled: '#',
  barHalf: '~',
  barEmpty: '-',
};

// ── Auto-detection ──────────────────────────────────────────

/**
 * Detect whether the terminal supports Unicode symbols.
 * Conservative: defaults to ASCII unless we're confident.
 */
function detectUnicodeSupport(): boolean {
  // CI environments often have limited terminal support
  if (process.env.CI) return false;

  // NO_COLOR explicitly set — use ASCII
  if (process.env.NO_COLOR) return false;

  // TERM=dumb — no special chars
  if (process.env.TERM === 'dumb') return false;

  // Common terminal types that support Unicode
  const term = (process.env.TERM ?? '').toLowerCase();
  if (term.includes('xterm') || term.includes('screen') || term.includes('tmux')) {
    return true;
  }

  // iTerm2, Kitty, WezTerm, Alacritty, Windows Terminal
  if (process.env.TERM_PROGRAM && (
    process.env.TERM_PROGRAM.includes('iTerm') ||
    process.env.TERM_PROGRAM.includes('WezTerm') ||
    process.env.TERM_PROGRAM.includes('kitty')
  )) {
    return true;
  }

  // LC_ALL / LANG contains UTF-8
  const lang = (process.env.LC_ALL ?? process.env.LANG ?? '').toLowerCase();
  if (lang.includes('utf-8') || lang.includes('utf8')) {
    return true;
  }

  // Default: assume no Unicode support
  return false;
}

// ── Public API ──────────────────────────────────────────────

/**
 * Get the current symbol set based on mode.
 * 'auto' detects from environment; 'unicode'/'ascii' force the mode.
 */
export function getSymbols(mode: SymbolMode = 'auto'): SymbolSet {
  switch (mode) {
    case 'unicode': return UNICODE;
    case 'ascii': return ASCII;
    case 'auto': return detectUnicodeSupport() ? UNICODE : ASCII;
  }
}

/**
 * Get the effective mode (resolves 'auto' to actual mode).
 */
export function resolveSymbolMode(mode: SymbolMode = 'auto'): 'unicode' | 'ascii' {
  if (mode !== 'auto') return mode;
  return detectUnicodeSupport() ? 'unicode' : 'ascii';
}
