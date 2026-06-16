import {describe, it, expect} from 'vitest';
import {getLineCol, validateAndTrimSubmit, navigateHistory} from './taskinput-utils.js';

// ── getLineCol ───────────────────────────────────────────────

describe('getLineCol', () => {
  it('returns line=1, col=length+1 for single-line text', () => {
    expect(getLineCol('hello')).toEqual({line: 1, col: 6});
  });

  it('returns line=1, col=1 for empty string', () => {
    expect(getLineCol('')).toEqual({line: 1, col: 1});
  });

  it('returns last line info for multi-line text', () => {
    expect(getLineCol('line1\nline2\nabc')).toEqual({line: 3, col: 4});
  });

  it('returns col=1 when last line is empty (trailing newline)', () => {
    expect(getLineCol('hello\n')).toEqual({line: 2, col: 1});
  });

  it('handles multiple trailing newlines', () => {
    expect(getLineCol('a\nb\n')).toEqual({line: 3, col: 1});
  });
});

// ── validateAndTrimSubmit ────────────────────────────────────

describe('validateAndTrimSubmit', () => {
  it('returns trimmed text for normal input', () => {
    expect(validateAndTrimSubmit('  hello world  ')).toBe('hello world');
  });

  it('returns null for whitespace-only input', () => {
    expect(validateAndTrimSubmit('   ')).toBeNull();
  });

  it('returns null for empty string', () => {
    expect(validateAndTrimSubmit('')).toBeNull();
  });

  it('returns trimmed text without leading/trailing whitespace', () => {
    expect(validateAndTrimSubmit('hello')).toBe('hello');
  });

  it('preserves internal whitespace', () => {
    expect(validateAndTrimSubmit('  hello   world  ')).toBe('hello   world');
  });
});

// ── navigateHistory ──────────────────────────────────────────

describe('navigateHistory', () => {
  it('enters history at index 0 on first Up press', () => {
    const result = navigateHistory('up', -1, 3, 'current draft', '');
    expect(result.nextIndex).toBe(0);
    expect(result.draftToSave).toBe('current draft');
  });

  it('increments index on subsequent Up', () => {
    const result = navigateHistory('up', 0, 3, '', '');
    expect(result.nextIndex).toBe(1);
    expect(result.draftToSave).toBeNull(); // draft already saved
  });

  it('caps at historyLength-1 on Up', () => {
    const result = navigateHistory('up', 2, 3, '', '');
    expect(result.nextIndex).toBe(2); // already at max
  });

  it('decrements index on Down', () => {
    const result = navigateHistory('down', 2, 3, '', '');
    expect(result.nextIndex).toBe(1);
  });

  it('exits history on Down from index 0', () => {
    const result = navigateHistory('down', 0, 3, '', 'my draft');
    expect(result.nextIndex).toBe(-1);
    expect(result.nextValue).toBe('my draft');
  });

  it('no-ops for empty history', () => {
    const result = navigateHistory('up', -1, 0, 'typing', '');
    expect(result.nextIndex).toBe(-1);
    expect(result.nextValue).toBe('typing');
    expect(result.draftToSave).toBeNull();
  });
});
