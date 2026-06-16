import {describe, it, expect} from 'vitest';
import {buildSegments, selectSegments} from './StatusBar.js';
import type {ConnectionState} from '../grpc/types.js';

// ── Helpers ────────────────────────────────────────────────

const defaultArgs = {
  connectionState: 'connected' as ConnectionState,
  isStreaming: false,
  workerId: 'grpc-worker',
  backend: 'grpc',
  progress: {completed: 2, total: 5},
  focusedArea: 'input' as const,
  activeMainPane: 'chat' as const,
  retryCount: 0,
  focusedAreaHelp: 'S-Tab focus  ? help',
};

// ── buildSegments ──────────────────────────────────────────

describe('buildSegments', () => {
  it('produces segments in priority order when connected', () => {
    const segs = buildSegments(defaultArgs);
    const ids = segs.map((s) => s.id);
    expect(ids).toEqual(['brand', 'connection', 'worker', 'backend', 'progress', 'focus', 'view', 'help']);
  });

  it('includes retry segment when error + retrying', () => {
    const segs = buildSegments({
      ...defaultArgs,
      connectionState: 'error',
      retryCount: 3,
    });
    const ids = segs.map((s) => s.id);
    expect(ids).toContain('retry');
    // view and retry can coexist (retry is higher priority, but both may fit)
  });

  it('shows "C-R reconnect" segment when offline and not retrying', () => {
    const segs = buildSegments({
      ...defaultArgs,
      connectionState: 'error',
      retryCount: 0,
    });
    const retrySeg = segs.find((s) => s.id === 'retry');
    expect(retrySeg).toBeDefined();
  });

  it('uses "grpc" label when connected', () => {
    const segs = buildSegments(defaultArgs);
    const connSeg = segs.find((s) => s.id === 'connection')!;
    expect(connSeg.width).toBe(6); // "● grpc" = 1 + 1 + 4
  });

  it('uses "offline" label when not connected', () => {
    const segs = buildSegments({
      ...defaultArgs,
      connectionState: 'error',
    });
    const connSeg = segs.find((s) => s.id === 'connection')!;
    expect(connSeg.width).toBe(9); // "✗ offline" = dot(1) + space(1) + "offline"(7) = 9
  });

  it('progress segment width scales with numbers', () => {
    const segs1 = buildSegments({...defaultArgs, progress: {completed: 0, total: 0}});
    const segs2 = buildSegments({...defaultArgs, progress: {completed: 12, total: 99}});
    const prog1 = segs1.find((s) => s.id === 'progress')!;
    const prog2 = segs2.find((s) => s.id === 'progress')!;
    expect(prog2.width).toBeGreaterThan(prog1.width);
  });

  it('omits help segment when help text is empty', () => {
    const segs = buildSegments({...defaultArgs, focusedAreaHelp: ''});
    expect(segs.find((s) => s.id === 'help')).toBeUndefined();
  });
});

// ── selectSegments ─────────────────────────────────────────

describe('selectSegments', () => {
  it('returns all segments when budget is sufficient', () => {
    const segs = buildSegments(defaultArgs);
    // Sum up all widths + 2 (padding)
    const totalWidth = segs.reduce((sum, s) => sum + s.width, 0) + 2;
    const selected = selectSegments(segs, totalWidth + 10);
    expect(selected).toHaveLength(segs.length);
  });

  it('truncates from the end when budget is tight', () => {
    const segs = buildSegments(defaultArgs);
    // Only allow first 3 segments + padding
    const budget = 2 + segs[0].width + segs[1].width + segs[2].width;
    const selected = selectSegments(segs, budget);
    expect(selected).toHaveLength(3);
    expect(selected[0].id).toBe('brand');
    expect(selected[1].id).toBe('connection');
    expect(selected[2].id).toBe('worker');
  });

  it('always shows brand + connection segment even on narrow terminals (60 cols)', () => {
    const segs = buildSegments({
      ...defaultArgs,
      connectionState: 'error',
      retryCount: 3,
      focusedAreaHelp: '',
    });
    const selected = selectSegments(segs, 60);
    expect(selected.length).toBeGreaterThan(0);
    expect(selected[0].id).toBe('brand');
    expect(selected.map((s) => s.id)).toContain('connection');
  });

  it('fits within 80 columns for connected state', () => {
    const segs = buildSegments(defaultArgs);
    const selected = selectSegments(segs, 80);
    const usedWidth = selected.reduce((sum, s) => sum + s.width, 0) + 2; // +2 padding
    expect(usedWidth).toBeLessThanOrEqual(80);
  });

  it('fits within 80 columns for error+retry state', () => {
    const segs = buildSegments({
      ...defaultArgs,
      connectionState: 'error',
      retryCount: 3,
    });
    const selected = selectSegments(segs, 80);
    const usedWidth = selected.reduce((sum, s) => sum + s.width, 0) + 2;
    expect(usedWidth).toBeLessThanOrEqual(80);
  });

  it('fits within 60 columns (narrow terminal)', () => {
    const segs = buildSegments({
      ...defaultArgs,
      connectionState: 'error',
      retryCount: 2,
      focusedAreaHelp: '',
    });
    const selected = selectSegments(segs, 60);
    const usedWidth = selected.reduce((sum, s) => sum + s.width, 0) + 2;
    expect(usedWidth).toBeLessThanOrEqual(60);
    // Should at least have connection + progress + focus
    const ids = selected.map((s) => s.id);
    expect(ids).toContain('connection');
    expect(ids).toContain('progress');
  });

  it('returns empty array when budget is too small for any segment', () => {
    const segs = buildSegments(defaultArgs);
    const selected = selectSegments(segs, 5); // too small
    // Connection segment alone needs ~6 cols, so it should be empty
    expect(selected).toHaveLength(0);
  });
});
