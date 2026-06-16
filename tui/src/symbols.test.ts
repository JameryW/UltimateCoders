import {describe, it, expect, vi, beforeEach, afterEach} from 'vitest';
import {getSymbols, resolveSymbolMode} from './symbols.js';

describe('getSymbols', () => {
  it('returns unicode symbols in unicode mode', () => {
    const s = getSymbols('unicode');
    expect(s.pending).toBe('○');
    expect(s.completed).toBe('●');
    expect(s.inProgress).toBe('◉');
    expect(s.failed).toBe('✗');
    expect(s.divider).toBe('─');
    expect(s.verticalSep).toBe('│');
    expect(s.arrowRight).toBe('→');
  });

  it('returns ascii symbols in ascii mode', () => {
    const s = getSymbols('ascii');
    expect(s.pending).toBe('[ ]');
    expect(s.completed).toBe('[x]');
    expect(s.inProgress).toBe('[*]');
    expect(s.failed).toBe('[!]');
    expect(s.divider).toBe('-');
    expect(s.verticalSep).toBe('|');
    expect(s.arrowRight).toBe('->');
  });
});

describe('getSymbols: auto mode', () => {
  beforeEach(() => {
    vi.stubEnv('CI', '');
    vi.stubEnv('NO_COLOR', '');
    vi.stubEnv('TERM', 'xterm-256color');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('falls back to ascii in CI', () => {
    vi.stubEnv('CI', 'true');
    const s = getSymbols('auto');
    expect(s.pending).toBe('[ ]');
  });

  it('falls back to ascii with NO_COLOR', () => {
    vi.stubEnv('NO_COLOR', '1');
    const s = getSymbols('auto');
    expect(s.pending).toBe('[ ]');
  });

  it('falls back to ascii with TERM=dumb', () => {
    vi.stubEnv('TERM', 'dumb');
    const s = getSymbols('auto');
    expect(s.pending).toBe('[ ]');
  });

  it('uses unicode with TERM=xterm-256color', () => {
    vi.stubEnv('TERM', 'xterm-256color');
    const s = getSymbols('auto');
    expect(s.pending).toBe('○');
  });

  it('uses unicode with TERM=xterm', () => {
    vi.stubEnv('TERM', 'xterm');
    const s = getSymbols('auto');
    expect(s.pending).toBe('○');
  });
});

describe('resolveSymbolMode', () => {
  beforeEach(() => {
    vi.stubEnv('CI', '');
    vi.stubEnv('NO_COLOR', '');
    vi.stubEnv('TERM', 'xterm-256color');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns unicode when forced', () => {
    expect(resolveSymbolMode('unicode')).toBe('unicode');
  });

  it('returns ascii when forced', () => {
    expect(resolveSymbolMode('ascii')).toBe('ascii');
  });

  it('resolves auto in CI to ascii', () => {
    vi.stubEnv('CI', 'true');
    expect(resolveSymbolMode('auto')).toBe('ascii');
  });

  it('resolves auto with NO_COLOR to ascii', () => {
    vi.stubEnv('NO_COLOR', '1');
    expect(resolveSymbolMode('auto')).toBe('ascii');
  });

  it('resolves auto with TERM=xterm-256color to unicode', () => {
    vi.stubEnv('TERM', 'xterm-256color');
    expect(resolveSymbolMode('auto')).toBe('unicode');
  });
});
