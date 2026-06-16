import {describe, it, expect} from 'vitest';
import {isUnavailableError, getErrorMessage} from './useGrpcClient.js';

// ── isUnavailableError ───────────────────────────────────────

describe('isUnavailableError', () => {
  it('returns true for error with code 14 (UNAVAILABLE)', () => {
    expect(isUnavailableError({code: 14})).toBe(true);
  });

  it('returns false for error with different code', () => {
    expect(isUnavailableError({code: 2})).toBe(false);
    expect(isUnavailableError({code: 0})).toBe(false);
  });

  it('returns false for error without code property', () => {
    expect(isUnavailableError({message: 'oops'})).toBe(false);
  });

  it('returns false for null', () => {
    expect(isUnavailableError(null)).toBe(false);
  });

  it('returns false for undefined', () => {
    expect(isUnavailableError(undefined)).toBe(false);
  });

  it('returns false for string', () => {
    expect(isUnavailableError('error')).toBe(false);
  });

  it('returns false for code as string "14"', () => {
    expect(isUnavailableError({code: '14'})).toBe(false);
  });
});

// ── getErrorMessage ──────────────────────────────────────────

describe('getErrorMessage', () => {
  it('returns message from error with message property', () => {
    expect(getErrorMessage({message: 'Connection refused'})).toBe('Connection refused');
  });

  it('truncates long messages to 100 characters', () => {
    const longMsg = 'A'.repeat(200);
    const result = getErrorMessage({message: longMsg});
    expect(result).toHaveLength(100);
    expect(result).toBe('A'.repeat(100));
  });

  it('returns gRPC error code string when no message', () => {
    expect(getErrorMessage({code: 14})).toBe('gRPC error code 14');
  });

  it('prefers message over code when both present', () => {
    expect(getErrorMessage({code: 14, message: 'UNAVAILABLE'})).toBe('UNAVAILABLE');
  });

  it('returns String(err) for non-object errors', () => {
    expect(getErrorMessage('some error')).toBe('some error');
  });

  it('truncates String(err) to 100 characters', () => {
    const longStr = 'x'.repeat(200);
    const result = getErrorMessage(longStr);
    expect(result).toHaveLength(100);
  });

  it('handles null gracefully', () => {
    expect(getErrorMessage(null)).toBe('null');
  });

  it('handles undefined gracefully', () => {
    expect(getErrorMessage(undefined)).toBe('undefined');
  });
});
