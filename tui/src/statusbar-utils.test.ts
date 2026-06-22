import {describe, it, expect} from 'vitest';
import {
  getConnectionIndicator,
  getConnDetail,
  getLayoutMode,
} from './statusbar-utils.js';

// ── getConnectionIndicator ────────────────────────────────────

describe('getConnectionIndicator', () => {
  it('returns green filled dot for connected+streaming', () => {
    expect(getConnectionIndicator('connected', true)).toEqual({
      dot: '●', color: 'green',
    });
  });

  it('returns yellow hollow dot for connected+not-streaming', () => {
    expect(getConnectionIndicator('connected', false)).toEqual({
      dot: '○', color: 'yellow',
    });
  });

  it('returns yellow dotted circle for connecting', () => {
    expect(getConnectionIndicator('connecting', false)).toEqual({
      dot: '◌', color: 'yellow',
    });
  });

  it('returns red X for error', () => {
    expect(getConnectionIndicator('error', false)).toEqual({
      dot: '✗', color: 'red',
    });
  });

  it('returns red hollow dot for disconnected', () => {
    expect(getConnectionIndicator('disconnected', false)).toEqual({
      dot: '○', color: 'red',
    });
  });
});

// ── getConnDetail ────────────────────────────────────────────

describe('getConnDetail', () => {
  const now = 1000000;

  it('returns empty string for connected state', () => {
    expect(getConnDetail('connected', 'localhost:50051', 0, null, now)).toBe('');
  });

  it('returns server address for disconnected state', () => {
    expect(getConnDetail('disconnected', 'localhost:50051', 0, null, now)).toBe(' localhost:50051');
  });

  it('returns error detail with server address only', () => {
    expect(getConnDetail('error', 'localhost:50051', 0, null, now)).toBe(' localhost:50051');
  });

  it('includes retry count in error state', () => {
    expect(getConnDetail('error', 'localhost:50051', 2, null, now)).toBe(' localhost:50051 retry 2/5');
  });

  it('includes seconds remaining when nextRetryAt is set', () => {
    const nextRetryAt = now + 3000; // 3 seconds from now
    const result = getConnDetail('error', 'localhost:50051', 1, nextRetryAt, now);
    expect(result).toBe(' localhost:50051 retry 1/5 in 3s');
  });

  it('omits seconds when nextRetryAt is in the past (0 seconds)', () => {
    const nextRetryAt = now - 1000; // 1 second ago
    const result = getConnDetail('error', 'localhost:50051', 1, nextRetryAt, now);
    // Math.max(0, ...) prevents negative seconds, but 0 is not shown
    // (the "in Xs" part is only shown when retrySecondsLeft > 0)
    expect(result).toBe(' localhost:50051 retry 1/5');
  });

  it('returns server address for connecting state', () => {
    expect(getConnDetail('connecting', 'localhost:50051', 0, null, now)).toBe(' localhost:50051');
  });
});

// ── getLayoutMode ────────────────────────────────────────────

describe('getLayoutMode', () => {
  it('returns "narrow" for width < 80', () => {
    expect(getLayoutMode(79)).toBe('narrow');
    expect(getLayoutMode(40)).toBe('narrow');
  });

  it('returns "medium" for width 80-119', () => {
    expect(getLayoutMode(80)).toBe('medium');
    expect(getLayoutMode(99)).toBe('medium');
    expect(getLayoutMode(119)).toBe('medium');
  });

  it('returns "wide" for width >= 120', () => {
    expect(getLayoutMode(120)).toBe('wide');
    expect(getLayoutMode(200)).toBe('wide');
  });

  it('boundary at 79 is narrow', () => {
    expect(getLayoutMode(79)).toBe('narrow');
  });

  it('boundary at 80 is medium', () => {
    expect(getLayoutMode(80)).toBe('medium');
  });
});
