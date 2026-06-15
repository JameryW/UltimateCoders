import {describe, it, expect} from 'vitest';
import stringWidth from 'string-width';
import GraphemeSplitter from 'grapheme-splitter';

const splitter = new GraphemeSplitter();

function truncateToWidth(text: string, maxDisplayWidth: number): string {
  if (stringWidth(text) <= maxDisplayWidth) return text;
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

describe('truncateToWidth', () => {
  it('does not truncate short text', () => {
    expect(truncateToWidth('hello', 10)).toBe('hello');
  });

  it('does not truncate text exactly at width', () => {
    expect(truncateToWidth('hello', 5)).toBe('hello');
  });

  it('truncates long English text and adds ellipsis', () => {
    const result = truncateToWidth('hello world this is long', 10);
    expect(stringWidth(result)).toBeLessThanOrEqual(10);
    expect(result.endsWith('…')).toBe(true);
  });

  it('handles CJK characters correctly (2 columns each)', () => {
    // Each CJK char = 2 display columns
    const text = '中文输入法测试'; // 6 chars × 2 cols = 12 display cols
    const result = truncateToWidth(text, 7);
    expect(stringWidth(result)).toBeLessThanOrEqual(7);
    expect(result.endsWith('…')).toBe(true);
  });

  it('does not split combining characters', () => {
    // é can be represented as e + combining acute accent (2 code units, 1 grapheme)
    const text = 'ééééé'; // 5 × é (combining)
    const result = truncateToWidth(text, 3);
    // Should not split a combining character
    expect(result.endsWith('…')).toBe(true);
    // The part before … should be whole é characters
    const before = result.slice(0, -1); // remove …
    const graphemes = splitter.splitGraphemes(before);
    // Each grapheme should be 'é' (e + combining accent)
    for (const g of graphemes) {
      expect(g.length).toBe(2); // e + combining accent
    }
  });

  it('does not split ZWJ emoji', () => {
    // 👨‍👩‍👧 is a ZWJ sequence (family emoji)
    const family = '👨‍👩‍👧';
    const text = family + family + family;
    const result = truncateToWidth(text, 4);
    expect(result.endsWith('…')).toBe(true);
    // The part before … should be whole emoji
    const before = result.slice(0, -1);
    const graphemes = splitter.splitGraphemes(before);
    for (const g of graphemes) {
      // Each should be the full family emoji, not a partial
      expect(g).toBe(family);
    }
  });

  it('handles empty string', () => {
    expect(truncateToWidth('', 10)).toBe('');
  });

  it('handles width of 1', () => {
    const result = truncateToWidth('hello', 1);
    expect(stringWidth(result)).toBeLessThanOrEqual(1);
  });
});
