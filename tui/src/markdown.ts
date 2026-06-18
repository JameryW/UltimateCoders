/**
 * Markdown rendering for terminal — converts markdown text to ANSI-styled strings.
 *
 * Uses `marked` for parsing + `marked-terminal` for ANSI output.
 * Ink's `<Text>` component renders ANSI strings natively.
 *
 * Features:
 * - Code block syntax highlighting via cli-highlight
 * - Diff view coloring (+/- lines)
 * - Renderer caching by width
 *
 * ponytail: lazy init to avoid crashes in test environments.
 */
import {Marked} from 'marked';
import markedTerminal from 'marked-terminal';

// ── Code syntax highlighting ────────────────────────────────────

let _highlight: ((code: string, opts: any) => string) | null = null;

function getHighlight(): ((code: string, opts: any) => string) | null {
  if (_highlight !== null) return _highlight;
  try {
    _highlight = require('cli-highlight').highlight;
    return _highlight;
  } catch {
    _highlight = null;
    return null;
  }
}

function highlightCode(code: string, lang?: string): string {
  const highlight = getHighlight();
  if (!highlight) return code;
  try {
    return highlight(code, {
      language: lang && lang !== '' ? lang : undefined,
      ignoreIllegals: true,
    });
  } catch {
    return code;
  }
}

// ── Diff coloring ────────────────────────────────────────────────

export function colorDiff(text: string): string {
  const lines = text.split('\n');
  return lines.map((line) => {
    if (line.startsWith('+++') || line.startsWith('---')) {
      return `\x1b[1m\x1b[33m${line}\x1b[39m\x1b[22m`;
    }
    if (line.startsWith('@@')) {
      return `\x1b[36m${line}\x1b[39m`;
    }
    if (line.startsWith('+')) {
      return `\x1b[32m${line}\x1b[39m`;
    }
    if (line.startsWith('-')) {
      return `\x1b[31m${line}\x1b[39m`;
    }
    return line;
  }).join('\n');
}

export function isDiffText(text: string): boolean {
  if (!text) return false;
  const lines = text.split('\n').slice(0, 5);
  return lines.some((l) => l.startsWith('@@') || l.startsWith('diff --git') || l.startsWith('+++'));
}

// ── Markdown renderer cache ─────────────────────────────────────

const _markedCache = new Map<number, Marked>();

function getMarked(width?: number): Marked {
  const w = width ?? 80;
  const cached = _markedCache.get(w);
  if (cached) return cached;

  const opts = markedTerminal({
    width: w,
    reflowText: true,
    showLink: false,
    heading: ['magenta', 'bold', 'underline'],
    firstHeading: ['magenta', 'bold', 'underline'],
    strong: ['bold'],
    em: ['italic'],
    code: ['yellow'],
    codespan: ['yellow'],
    blockquote: ['gray'],
    listitem: ['cyan'],
    table: ['gray'],
    paragraph: [],
    highlight: (code: string, lang?: string) => highlightCode(code, lang),
  });
  const instance = new Marked(opts);
  if (_markedCache.size >= 5) {
    const firstKey = _markedCache.keys().next().value;
    if (firstKey !== undefined) _markedCache.delete(firstKey);
  }
  _markedCache.set(w, instance);
  return instance;
}

export function renderMarkdown(text: string, width?: number): string {
  if (!text || text.trim().length === 0) return text;
  try {
    const instance = getMarked(width);
    const result = instance.parse(text) as string;
    return result.trim();
  } catch {
    return text;
  }
}

export function hasMarkdown(text: string): boolean {
  if (!text) return false;
  return /^#{1,6}\s/m.test(text) ||
    /\*\*[^*]+\*\*/.test(text) ||
    /__[^_]+__/.test(text) ||
    /\*[^*]+\*/.test(text) ||
    /_[^_]+_/.test(text) ||
    /```/.test(text) ||
    /`[^`]+`/.test(text) ||
    /^[-*]\s/m.test(text) ||
    /^\d+\.\s/m.test(text);
}
