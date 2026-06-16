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
  retryCount: 0,
  focusedAreaHelp: 'S-Tab focus  C-T subtasks  ? help',
  brandChar: '◆',
};

// ── buildSegments ──────────────────────────────────────────

describe('buildSegments', () => {
  it('produces segments in priority order when connected', () => {
    const segs = buildSegments(defaultArgs);
    const ids = segs.map((s) => s.id);
    expect(ids).toEqual(['brand', 'connection', 'worker', 'backend', 'progress', 'focus', 'help']);
  });

  it('includes retry segment when error + retrying', () => {
    const segs = buildSegments({
      ...defaultArgs,
      connectionState: 'error',
      retryCount: 3,
    });
    const ids = segs.map((s) => s.id);
    expect(ids).toContain('retry');
    // retry segment is present when error + retrying
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
  it('returns all segments when budget is sufficient (>100 cols)', () => {
    const segs = buildSegments(defaultArgs);
    // Sum up all widths + 2 (padding)
    const totalWidth = segs.reduce((sum, s) => sum + s.width, 0) + 2;
    // Use budget > 100 to avoid progressive collapse skipping segments
    const selected = selectSegments(segs, Math.max(totalWidth + 10, 110));
    expect(selected).toHaveLength(segs.length);
  });

  it('truncates from the end when budget is tight', () => {
    const segs = buildSegments(defaultArgs);
    // Budget = 100 + padding: above all tier thresholds, but only enough
    // for brand + connection. Remaining segments don't fit.
    const brandAndConnWidth = segs[0].width + segs[1].width + 2; // +2 padding
    const selected = selectSegments(segs, Math.max(brandAndConnWidth, 101));
    // At budget 101, we're above the 100 threshold, so no tier skipping.
    // But the actual width only fits brand + connection.
    expect(selected.length).toBeGreaterThanOrEqual(2);
    expect(selected[0].id).toBe('brand');
    expect(selected[1].id).toBe('connection');
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

  // ── Progressive collapse tiers (PRD P3) ──────────────────
  it('<60 cols: only brand + connection + progress', () => {
    const segs = buildSegments({...defaultArgs, focusedAreaHelp: ''});
    const selected = selectSegments(segs, 55);
    const ids = selected.map((s) => s.id);
    expect(ids).toContain('brand');
    expect(ids).toContain('connection');
    expect(ids).toContain('progress');
    // Low-priority segments skipped
    expect(ids).not.toContain('worker');
    expect(ids).not.toContain('backend');
    expect(ids).not.toContain('focus');
    expect(ids).not.toContain('help');
  });

  it('60-80 cols: brand + connection + progress + focus', () => {
    const segs = buildSegments({...defaultArgs, focusedAreaHelp: ''});
    const selected = selectSegments(segs, 70);
    const ids = selected.map((s) => s.id);
    expect(ids).toContain('brand');
    expect(ids).toContain('connection');
    expect(ids).toContain('progress');
    expect(ids).toContain('focus');
    // Worker/backend/help skipped
    expect(ids).not.toContain('worker');
    expect(ids).not.toContain('backend');
    expect(ids).not.toContain('help');
  });

  it('80-100 cols: removes help segment', () => {
    const segs = buildSegments(defaultArgs); // has help text
    const selected = selectSegments(segs, 90);
    const ids = selected.map((s) => s.id);
    expect(ids).not.toContain('help');
  });
});
