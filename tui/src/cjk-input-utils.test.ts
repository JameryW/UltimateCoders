import {describe, it, expect} from 'vitest';
import {
  inverseChar,
  dimText,
  insertAtCursor,
  deleteBackward,
  deleteToEnd,
  renderInputWithCursor,
  cursorDisplayCol,
  wordBoundaryBackward,
  wordBoundaryForward,
} from './cjk-input-utils.js';

// ── inverseChar ──────────────────────────────────────────────

describe('inverseChar', () => {
  it('wraps an ASCII character in inverse-video escape sequences', () => {
    expect(inverseChar('A')).toBe('\x1B[7mA\x1B[27m');
  });

  it('wraps a CJK character correctly', () => {
    expect(inverseChar('你')).toBe('\x1B[7m你\x1B[27m');
  });

  it('wraps a space character', () => {
    expect(inverseChar(' ')).toBe('\x1B[7m \x1B[27m');
  });

  it('wraps a multi-codepoint emoji (ZWJ sequence) as one unit', () => {
    // 👨‍💻 = man technologist (ZWJ sequence)
    expect(inverseChar('👨‍💻')).toBe('\x1B[7m👨‍💻\x1B[27m');
  });

  it('wraps an empty string (edge case)', () => {
    expect(inverseChar('')).toBe('\x1B[7m\x1B[27m');
  });
});

// ── dimText ──────────────────────────────────────────────────

describe('dimText', () => {
  it('wraps text in dim escape sequences', () => {
    expect(dimText('hello')).toBe('\x1B[2mhello\x1B[22m');
  });

  it('wraps empty string', () => {
    expect(dimText('')).toBe('\x1B[2m\x1B[22m');
  });

  it('wraps CJK text', () => {
    expect(dimText('你好')).toBe('\x1B[2m你好\x1B[22m');
  });

  it('wraps multi-line text', () => {
    expect(dimText('a\nb')).toBe('\x1B[2ma\nb\x1B[22m');
  });
});

// ── insertAtCursor ───────────────────────────────────────────

describe('insertAtCursor', () => {
  it('inserts ASCII char at beginning', () => {
    const result = insertAtCursor('bc', 0, 'a');
    expect(result).toEqual({nextValue: 'abc', nextCursorGI: 1});
  });

  it('inserts ASCII char in the middle', () => {
    const result = insertAtCursor('ac', 1, 'b');
    expect(result).toEqual({nextValue: 'abc', nextCursorGI: 2});
  });

  it('inserts ASCII char at the end', () => {
    const result = insertAtCursor('ab', 2, 'c');
    expect(result).toEqual({nextValue: 'abc', nextCursorGI: 3});
  });

  it('inserts into empty string', () => {
    const result = insertAtCursor('', 0, 'x');
    expect(result).toEqual({nextValue: 'x', nextCursorGI: 1});
  });

  it('inserts CJK character (single grapheme)', () => {
    const result = insertAtCursor('abc', 1, '中');
    expect(result).toEqual({nextValue: 'a中bc', nextCursorGI: 2});
  });

  it('inserts multi-grapheme input (pasted text)', () => {
    const result = insertAtCursor('ac', 1, 'bd');
    expect(result).toEqual({nextValue: 'abdc', nextCursorGI: 3});
  });

  it('inserts newline for Ctrl+J', () => {
    const result = insertAtCursor('abc', 2, '\n');
    expect(result).toEqual({nextValue: 'ab\nc', nextCursorGI: 3});
  });

  it('inserts two spaces for Tab', () => {
    const result = insertAtCursor('ab', 1, '  ');
    expect(result).toEqual({nextValue: 'a  b', nextCursorGI: 3});
  });

  it('inserts emoji (ZWJ sequence) as input', () => {
    // 👨‍💻 is a single grapheme cluster
    const result = insertAtCursor('ab', 1, '👨‍💻');
    expect(result).toEqual({nextValue: 'a👨‍💻b', nextCursorGI: 2});
  });
});

// ── deleteBackward ────────────────────────────────────────────

describe('deleteBackward', () => {
  it('deletes single ASCII char before cursor', () => {
    const result = deleteBackward('abc', 2);
    expect(result).toEqual({nextValue: 'ac', nextCursorGI: 1});
  });

  it('deletes CJK character as whole grapheme', () => {
    const result = deleteBackward('a你b', 2);
    expect(result).toEqual({nextValue: 'ab', nextCursorGI: 1});
  });

  it('deletes at cursor position 1 (delete first char)', () => {
    const result = deleteBackward('abc', 1);
    expect(result).toEqual({nextValue: 'bc', nextCursorGI: 0});
  });

  it('returns null when cursor at position 0 (nothing to delete)', () => {
    const result = deleteBackward('abc', 0);
    expect(result).toBeNull();
  });

  it('deletes from single-character string', () => {
    const result = deleteBackward('x', 1);
    expect(result).toEqual({nextValue: '', nextCursorGI: 0});
  });

  it('deletes from empty string at cursor 0', () => {
    const result = deleteBackward('', 0);
    expect(result).toBeNull();
  });

  it('deletes emoji (ZWJ sequence) as one grapheme', () => {
    // "a👨‍💻b" has 3 graphemes: "a", "👨‍💻", "b"
    const result = deleteBackward('a👨‍💻b', 2);
    expect(result).toEqual({nextValue: 'ab', nextCursorGI: 1});
  });

  it('deletes combining character with its base', () => {
    // "é" = é (e + combining acute) is one grapheme
    const result = deleteBackward('aéb', 2);
    expect(result).toEqual({nextValue: 'ab', nextCursorGI: 1});
  });
});

// ── deleteToEnd ──────────────────────────────────────────────

describe('deleteToEnd', () => {
  it('deletes from middle to end', () => {
    const result = deleteToEnd('abcde', 2);
    expect(result).toEqual({nextValue: 'ab', nextCursorGI: 2});
  });

  it('deletes from position 0 (clears all)', () => {
    const result = deleteToEnd('abc', 0);
    expect(result).toEqual({nextValue: '', nextCursorGI: 0});
  });

  it('at end position returns same value (no-op)', () => {
    const result = deleteToEnd('abc', 3);
    expect(result).toEqual({nextValue: 'abc', nextCursorGI: 3});
  });

  it('handles CJK characters correctly', () => {
    const result = deleteToEnd('你好看', 1);
    expect(result).toEqual({nextValue: '你', nextCursorGI: 1});
  });
});

// ── renderInputWithCursor ────────────────────────────────────

describe('renderInputWithCursor', () => {
  it('renders cursor at end as inverse space block', () => {
    const result = renderInputWithCursor('abc', 3, true, true, '');
    expect(result).toBe('abc\x1B[7m \x1B[27m');
  });

  it('renders cursor in middle as inverse grapheme', () => {
    const result = renderInputWithCursor('abc', 1, true, true, '');
    expect(result).toBe('a\x1B[7mb\x1B[27mc');
  });

  it('renders cursor at beginning as inverse first grapheme', () => {
    const result = renderInputWithCursor('abc', 0, true, true, '');
    expect(result).toBe('\x1B[7ma\x1B[27mbc');
  });

  it('renders empty value with placeholder - focused with cursor', () => {
    const result = renderInputWithCursor('', 0, true, true, 'Enter...');
    expect(result).toBe('\x1B[7mE\x1B[27m\x1B[2mnter...\x1B[22m');
  });

  it('renders empty value with placeholder - unfocused', () => {
    const result = renderInputWithCursor('', 0, true, false, 'Enter...');
    expect(result).toBe('\x1B[2mEnter...\x1B[22m');
  });

  it('renders empty value without placeholder - focused', () => {
    const result = renderInputWithCursor('', 0, true, true, '');
    expect(result).toBe('\x1B[7m \x1B[27m');
  });

  it('renders plain text when showCursor is false', () => {
    const result = renderInputWithCursor('abc', 1, false, true, '');
    expect(result).toBe('abc');
  });

  it('renders plain text when focus is false', () => {
    const result = renderInputWithCursor('abc', 1, true, false, '');
    expect(result).toBe('abc');
  });

  it('renders CJK character at cursor position with inverse', () => {
    const result = renderInputWithCursor('你好', 0, true, true, '');
    expect(result).toBe('\x1B[7m你\x1B[27m好');
  });

  it('renders CJK cursor at end with inverse space block', () => {
    const result = renderInputWithCursor('你好', 2, true, true, '');
    expect(result).toBe('你好\x1B[7m \x1B[27m');
  });
});

// ── cursorDisplayCol ─────────────────────────────────────────

describe('cursorDisplayCol', () => {
  it('returns 0 for cursor at beginning', () => {
    expect(cursorDisplayCol('abc', 0)).toBe(0);
  });

  it('returns string width for cursor at end of ASCII', () => {
    expect(cursorDisplayCol('abc', 3)).toBe(3);
  });

  it('counts CJK characters as 2 columns each', () => {
    // "你好" = 2 graphemes, each 2 display cols wide
    expect(cursorDisplayCol('你好', 1)).toBe(2);
    expect(cursorDisplayCol('你好', 2)).toBe(4);
  });

  it('handles mixed ASCII and CJK', () => {
    // "a你好b" = 4 graphemes, display width = 1+2+2+1 = 6
    expect(cursorDisplayCol('a你好b', 2)).toBe(3); // "a你" = 1+2 = 3
    expect(cursorDisplayCol('a你好b', 4)).toBe(6);
  });

  it('returns 0 for empty string at cursor 0', () => {
    expect(cursorDisplayCol('', 0)).toBe(0);
  });
});

// ── Word Boundary Tests ────────────────────────────────────
describe('wordBoundaryBackward', () => {
  it('returns 0 when already at start', () => {
    expect(wordBoundaryBackward('hello', 0)).toBe(0);
  });

  it('skips whitespace then stops at word boundary', () => {
    expect(wordBoundaryBackward('hello world', 11)).toBe(6);
  });

  it('stops at CJK character boundary', () => {
    expect(wordBoundaryBackward('hello你好', 7)).toBe(6);
  });

  it('each CJK char is its own word', () => {
    expect(wordBoundaryBackward('你好世界', 3)).toBe(2);
  });

  it('skips multiple spaces', () => {
    expect(wordBoundaryBackward('hello   world', 13)).toBe(8);
  });
});

describe('wordBoundaryForward', () => {
  it('returns max when already at end', () => {
    expect(wordBoundaryForward('hello', 5)).toBe(5);
  });

  it('skips current word then whitespace', () => {
    expect(wordBoundaryForward('hello world', 0)).toBe(6);
  });

  it('each CJK char is its own word', () => {
    expect(wordBoundaryForward('你好世界', 0)).toBe(1);
  });

  it('skips CJK then lands on next word', () => {
    expect(wordBoundaryForward('你 hello', 0)).toBe(1);
  });
});
