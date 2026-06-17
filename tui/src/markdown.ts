/**
 * Markdown rendering for terminal — converts markdown text to ANSI-styled strings.
 *
 * Uses `marked` for parsing + `marked-terminal` for ANSI output.
 * Ink's `<Text>` component renders ANSI strings natively.
 *
 * ponytail: lazy init to avoid crashes in test environments.
 * Upgrade path: swap to ink-stream-markdown for streaming + per-node React rendering.
 */
import {Marked} from 'marked';
import markedTerminal from 'marked-terminal';

// ── Lazy singleton ────────────────────────────────────────────

let _marked: Marked | null = null;

function getMarked(width?: number): Marked {
  if (!_marked || width) {
    const opts = markedTerminal({
      width: width ?? 80,
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
    });
    _marked = new Marked(opts);
  }
  return _marked;
}

/**
 * Render markdown text to an ANSI-styled terminal string.
 * Safe in test environments — returns plain text if rendering fails.
 */
export function renderMarkdown(text: string, width?: number): string {
  if (!text || text.trim().length === 0) return text;
  try {
    const instance = getMarked(width);
    const result = instance.parse(text) as string;
    return result.trim();
  } catch {
    // Fallback: return original text if markdown rendering fails
    return text;
  }
}

/** Check if text looks like it contains markdown formatting. */
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
