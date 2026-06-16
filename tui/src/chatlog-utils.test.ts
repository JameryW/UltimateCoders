/**
 * Tests for ChatLog pure functions: getEventIcon, filterMessages, EVENT_ICONS.
 */
import {describe, it, expect} from 'vitest';
import {getEventIcon, EVENT_ICONS, filterMessages} from './components/ChatLog.js';
import type {ChatMessage} from './components/ChatLog.js';

// ── getEventIcon ────────────────────────────────────────────────

describe('getEventIcon', () => {
  it('returns empty string for undefined eventType', () => {
    expect(getEventIcon(undefined)).toBe('');
  });

  it('returns icon + space for known event types', () => {
    expect(getEventIcon('task_submitted')).toBe('📋 ');
    expect(getEventIcon('subtask_completed')).toBe('✓ ');
    expect(getEventIcon('subtask_failed')).toBe('✗ ');
    expect(getEventIcon('tool_call')).toBe('🔧 ');
  });

  it('returns empty string for unknown event types', () => {
    expect(getEventIcon('unknown_event')).toBe('');
  });

  it('every EVENT_ICONS entry produces icon + space', () => {
    for (const [eventType, icon] of Object.entries(EVENT_ICONS)) {
      expect(getEventIcon(eventType)).toBe(`${icon} `);
    }
  });
});

// ── EVENT_ICONS completeness ───────────────────────────────────

describe('EVENT_ICONS', () => {
  const EXPECTED_TYPES = [
    'task_submitted', 'task_completed', 'task_failed',
    'subtask_assigned', 'subtask_started', 'subtask_completed', 'subtask_failed',
    'tool_call', 'tool_result',
  ];

  it('covers all expected event types', () => {
    for (const t of EXPECTED_TYPES) {
      expect(EVENT_ICONS[t]).toBeDefined();
    }
  });

  it('all icons are non-empty single characters or emoji', () => {
    for (const [type, icon] of Object.entries(EVENT_ICONS)) {
      expect(icon.length).toBeGreaterThan(0);
    }
  });
});

// ── filterMessages (extended) ──────────────────────────────────

describe('filterMessages', () => {
  const makeMsg = (text: string, eventType?: string, isUser = false): ChatMessage => ({
    id: `test-${Math.random()}`,
    timestamp: '12:00',
    text,
    isUser,
    eventType,
  });

  it('passes user messages through all filters', () => {
    const msg = makeMsg('hello', undefined, true);
    for (const filter of ['all', 'task', 'subtask', 'tool', 'error'] as const) {
      expect(filterMessages([msg], filter)).toHaveLength(1);
    }
  });

  it('passes messages without eventType through all filters', () => {
    const msg = makeMsg('system msg');
    for (const filter of ['all', 'task', 'subtask', 'tool', 'error'] as const) {
      expect(filterMessages([msg], filter)).toHaveLength(1);
    }
  });

  it('error filter matches subtask_failed and task_failed', () => {
    const msgs = [
      makeMsg('failed1', 'subtask_failed'),
      makeMsg('failed2', 'task_failed'),
      makeMsg('started', 'subtask_started'),
    ];
    const result = filterMessages(msgs, 'error');
    expect(result).toHaveLength(2);
  });
});
