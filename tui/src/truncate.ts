/**
 * Truncate a string to fit within a maximum display width (terminal columns).
 *
 * Uses `string-width` for display width calculation and `GraphemeSplitter`
 * to ensure grapheme clusters (CJK, combining chars, ZWJ emoji) are never
 * split mid-cluster. Appends an ellipsis (…) when truncation occurs.
 */

import stringWidth from 'string-width';
import GraphemeSplitter from 'grapheme-splitter';

const splitter = new GraphemeSplitter();

export function truncateToWidth(text: string, maxDisplayWidth: number): string {
  if (stringWidth(text) <= maxDisplayWidth) return text;
  // Remove graphemes from end until width fits (leave room for "…")
  const ellipsisWidth = 1;
  const graphemes = splitter.splitGraphemes(text);
  let width = stringWidth(text);
  let end = graphemes.length;
  while (width > maxDisplayWidth - ellipsisWidth && end > 0) {
    end--;
    width -= stringWidth(graphemes[end]);
  }
  return graphemes.slice(0, end).join('') + '…';
}
