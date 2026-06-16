/**
 * Tests for SubtaskTree pure functions: getProgressText, buildProgressBar, buildDependedByMap.
 */
import {describe, it, expect} from 'vitest';
import {getProgressText, buildProgressBar, buildDependedByMap} from './components/SubtaskTree.js';
import type {SubtaskItem} from './components/SubtaskTree.js';

// ── getProgressText ────────────────────────────────────────────

describe('getProgressText', () => {
  it('shows 0% when no subtasks', () => {
    expect(getProgressText(0, 0)).toBe('0/0 0%');
  });

  it('shows correct percentage', () => {
    expect(getProgressText(3, 5)).toBe('3/5 60%');
  });

  it('shows 100% when all completed', () => {
    expect(getProgressText(5, 5)).toBe('5/5 100%');
  });

  it('rounds percentage', () => {
    expect(getProgressText(1, 3)).toBe('1/3 33%');
  });
});

// ── buildProgressBar ───────────────────────────────────────────

describe('buildProgressBar', () => {
  it('returns empty string when total is 0', () => {
    expect(buildProgressBar(0, 0, 10, '▓', '░')).toBe('');
  });

  it('renders fully filled bar', () => {
    expect(buildProgressBar(5, 5, 5, '#', '-')).toBe('##### 100%');
  });

  it('renders partially filled bar', () => {
    expect(buildProgressBar(1, 4, 4, '▓', '░')).toBe('▓░░░ 25%');
  });

  it('renders empty bar at 0%', () => {
    expect(buildProgressBar(0, 3, 3, '#', '-')).toBe('--- 0%');
  });

  it('handles barWidth of 1', () => {
    expect(buildProgressBar(0, 2, 1, '#', '-')).toBe('- 0%');
    expect(buildProgressBar(2, 2, 1, '#', '-')).toBe('# 100%');
  });
});

// ── buildDependedByMap ──────────────────────────────────────────

describe('buildDependedByMap', () => {
  const makeSubtask = (id: string, index: number, dependsOn?: string[]): SubtaskItem => ({
    id,
    index,
    description: `Subtask ${index}`,
    status: 'pending',
    dependsOn,
  });

  it('returns empty map when no subtasks', () => {
    const map = buildDependedByMap([]);
    expect(map.size).toBe(0);
  });

  it('returns empty map when no dependencies', () => {
    const subtasks = [
      makeSubtask('a', 1),
      makeSubtask('b', 2),
    ];
    const map = buildDependedByMap(subtasks);
    expect(map.size).toBe(0);
  });

  it('maps reverse dependency correctly', () => {
    // subtask 2 depends on subtask 1 → subtask 1 is depended by [2]
    const subtasks = [
      makeSubtask('a', 1),
      makeSubtask('b', 2, ['a']),
    ];
    const map = buildDependedByMap(subtasks);
    expect(map.get(0)).toEqual([2]);
  });

  it('handles multiple dependents', () => {
    // subtask 2 and 3 both depend on subtask 1
    const subtasks = [
      makeSubtask('a', 1),
      makeSubtask('b', 2, ['a']),
      makeSubtask('c', 3, ['a']),
    ];
    const map = buildDependedByMap(subtasks);
    expect(map.get(0)).toEqual([2, 3]);
  });

  it('ignores unknown dependency IDs', () => {
    const subtasks = [
      makeSubtask('a', 1, ['nonexistent']),
    ];
    const map = buildDependedByMap(subtasks);
    expect(map.size).toBe(0);
  });

  it('handles diamond dependency', () => {
    // 1 → 2,3 → 4
    const subtasks = [
      makeSubtask('a', 1),
      makeSubtask('b', 2, ['a']),
      makeSubtask('c', 3, ['a']),
      makeSubtask('d', 4, ['b', 'c']),
    ];
    const map = buildDependedByMap(subtasks);
    expect(map.get(0)).toEqual([2, 3]); // a is depended by b,c
    expect(map.get(1)).toEqual([4]);     // b is depended by d
    expect(map.get(2)).toEqual([4]);     // c is depended by d
  });
});
