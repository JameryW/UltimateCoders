import {describe, it, expect} from 'vitest';
import {mapSubtaskStatus, mapTaskStatus} from './types.js';
import type {SubtaskStatusType} from './types.js';

// ── mapSubtaskStatus ─────────────────────────────────────────

describe('mapSubtaskStatus', () => {
  it('maps "Pending" to "pending"', () => {
    expect(mapSubtaskStatus('Pending')).toBe('pending');
  });

  it('maps "Assigned" to "assigned"', () => {
    expect(mapSubtaskStatus('Assigned')).toBe('assigned');
  });

  it('maps "InProgress" to "in_progress"', () => {
    expect(mapSubtaskStatus('InProgress')).toBe('in_progress');
  });

  it('maps "Completed" to "completed"', () => {
    expect(mapSubtaskStatus('Completed')).toBe('completed');
  });

  it('maps "Failed" to "failed"', () => {
    expect(mapSubtaskStatus('Failed')).toBe('failed');
  });

  it('maps "Conflicted" to "conflicted"', () => {
    expect(mapSubtaskStatus('Conflicted')).toBe('conflicted');
  });

  it('defaults unknown status to "pending"', () => {
    expect(mapSubtaskStatus('UnknownStatus')).toBe('pending');
  });

  it('defaults empty string to "pending"', () => {
    expect(mapSubtaskStatus('')).toBe('pending');
  });

  it('is case-sensitive: lowercase "pending" defaults to "pending"', () => {
    // "pending" != "Pending", so it falls to default
    expect(mapSubtaskStatus('pending')).toBe('pending');
  });

  it('returns correct SubtaskStatusType for all valid values', () => {
    const statuses: Array<[string, SubtaskStatusType]> = [
      ['Pending', 'pending'],
      ['Assigned', 'assigned'],
      ['InProgress', 'in_progress'],
      ['Completed', 'completed'],
      ['Failed', 'failed'],
      ['Conflicted', 'conflicted'],
    ];
    for (const [proto, expected] of statuses) {
      expect(mapSubtaskStatus(proto)).toBe(expected);
    }
  });
});

// ── mapTaskStatus ────────────────────────────────────────────

describe('mapTaskStatus', () => {
  it('returns the proto status string unchanged', () => {
    expect(mapTaskStatus('InProgress')).toBe('InProgress');
  });

  it('returns empty string for empty input', () => {
    expect(mapTaskStatus('')).toBe('');
  });

  it('returns arbitrary string unchanged (identity function)', () => {
    expect(mapTaskStatus('anything')).toBe('anything');
  });
});
