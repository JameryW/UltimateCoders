import {describe, it, expect} from 'vitest';
import {decomposeDescription, cleanSubtaskDescription} from './offline-utils.js';

// ── decomposeDescription ─────────────────────────────────────

describe('decomposeDescription', () => {
  it('splits multi-line description into separate lines', () => {
    const result = decomposeDescription('Fix bug\nWrite tests\nDeploy');
    expect(result).toEqual(['Fix bug', 'Write tests', 'Deploy']);
  });

  it('duplicates single line to ensure at least 2 entries', () => {
    const result = decomposeDescription('Fix the login bug');
    expect(result).toEqual(['Fix the login bug', 'Fix the login bug']);
  });

  it('trims and filters empty lines', () => {
    // "  A  \n\n  B  \n  " → trim → ["A", "B", ""] → filter empty → ["A", "B"]
    // then length <= 1 is false (2 items), so no duplication
    const result = decomposeDescription('  A  \n\n  B  \n  ');
    expect(result).toEqual(['A', 'B']);
  });

  it('handles whitespace-only lines by filtering them', () => {
    const result = decomposeDescription('A\n   \nB');
    expect(result).toEqual(['A', 'B']);
  });

  it('handles empty string by duplicating the trimmed original', () => {
    const result = decomposeDescription('');
    expect(result).toEqual(['']);
  });
});

// ── cleanSubtaskDescription ──────────────────────────────────

describe('cleanSubtaskDescription', () => {
  it('strips "1. " prefix', () => {
    expect(cleanSubtaskDescription('1. Fix bug')).toBe('Fix bug');
  });

  it('strips "2) " prefix', () => {
    expect(cleanSubtaskDescription('2) Write tests')).toBe('Write tests');
  });

  it('strips multi-digit prefix "10. "', () => {
    expect(cleanSubtaskDescription('10. Deploy')).toBe('Deploy');
  });

  it('returns unchanged text when no number prefix', () => {
    expect(cleanSubtaskDescription('Fix the bug')).toBe('Fix the bug');
  });

  it('falls back to original if cleaning produces empty string', () => {
    // "1. " → after clean → "" → falls back to "1. "
    expect(cleanSubtaskDescription('1. ')).toBe('1. ');
  });

  it('handles "1)" style prefix', () => {
    expect(cleanSubtaskDescription('1)Do something')).toBe('Do something');
  });

  it('does not strip numbers in the middle', () => {
    expect(cleanSubtaskDescription('Fix bug 1. issue')).toBe('Fix bug 1. issue');
  });
});
