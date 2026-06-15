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

  it('auto mode falls back to ascii in CI', () => {
    process.env.CI = 'true';
    const s = getSymbols('auto');
    expect(s.pending).toBe('[ ]');
    delete process.env.CI;
  });

  it('auto mode falls back to ascii with NO_COLOR', () => {
    process.env.NO_COLOR = '1';
    const s = getSymbols('auto');
    expect(s.pending).toBe('[ ]');
    delete process.env.NO_COLOR;
  });

  it('auto mode falls back to ascii with TERM=dumb', () => {
    const orig = process.env.TERM;
    process.env.TERM = 'dumb';
    const s = getSymbols('auto');
    expect(s.pending).toBe('[ ]');
    process.env.TERM = orig;
  });

  it('auto mode uses unicode with TERM=xterm-256color', () => {
    const orig = process.env.TERM;
    process.env.TERM = 'xterm-256color';
    delete process.env.CI;
    delete process.env.NO_COLOR;
    const s = getSymbols('auto');
    expect(s.pending).toBe('○');
    process.env.TERM = orig;
  });
});

describe('resolveSymbolMode', () => {
  it('returns unicode when forced', () => {
    expect(resolveSymbolMode('unicode')).toBe('unicode');
  });

  it('returns ascii when forced', () => {
    expect(resolveSymbolMode('ascii')).toBe('ascii');
  });

  it('resolves auto in CI to ascii', () => {
    process.env.CI = 'true';
    expect(resolveSymbolMode('auto')).toBe('ascii');
    delete process.env.CI;
  });
});
