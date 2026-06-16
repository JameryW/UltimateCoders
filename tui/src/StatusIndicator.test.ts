import {describe, it, expect} from 'vitest';
import {formatElapsed} from './components/StatusIndicator.js';

describe('formatElapsed', () => {
  it('formats seconds: (5s)', () => {
    expect(formatElapsed(5000)).toBe('(5s)');
  });

  it('formats 0 seconds: (0s)', () => {
    expect(formatElapsed(0)).toBe('(0s)');
  });

  it('formats minutes + seconds: (1m 30s)', () => {
    expect(formatElapsed(90_000)).toBe('(1m 30s)');
  });

  it('pads seconds to 2 digits when >= 1 minute', () => {
    expect(formatElapsed(61_000)).toBe('(1m 01s)');
  });

  it('formats hours + minutes + seconds: (2h 15m 00s)', () => {
    expect(formatElapsed(2 * 3600_000 + 15 * 60_000)).toBe('(2h 15m 00s)');
  });
});
