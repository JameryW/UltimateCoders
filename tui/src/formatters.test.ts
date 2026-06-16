import {describe, it, expect} from 'vitest';
import {formatTaskEvent, formatTaskEvents} from './formatters.js';
import type {TaskEventProto} from './grpc/types.js';

function event(type: string, overrides?: Partial<TaskEventProto>): TaskEventProto {
  return {
    timestamp: '2026-06-15T12:00:00Z',
    type,
    taskId: 'task-abc123456789',
    subtaskId: 'subtask-xyz987',
    data: {},
    ...overrides,
  };
}

describe('formatTaskEvent', () => {
  it('formats task_submitted', () => {
    const msg = formatTaskEvent(event('task_submitted'));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('Task submitted');
    expect(msg!.text).toContain('task-abc');
    expect(msg!.color).toBe('cyan');
    expect(msg!.bold).toBe(true);
    expect(msg!.eventType).toBe('task_submitted');
  });

  it('formats subtask_assigned with worker', () => {
    const msg = formatTaskEvent(event('subtask_assigned', {
      data: {worker_id: 'worker-1'},
    }));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('worker-1');
    expect(msg!.color).toBe('blue');
    expect(msg!.eventType).toBe('subtask_assigned');
  });

  it('formats subtask_started', () => {
    const msg = formatTaskEvent(event('subtask_started'));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('started');
    expect(msg!.color).toBe('cyan');
  });

  it('formats subtask_completed', () => {
    const msg = formatTaskEvent(event('subtask_completed'));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('completed');
    expect(msg!.color).toBe('green');
  });

  it('formats subtask_failed with error', () => {
    const msg = formatTaskEvent(event('subtask_failed', {
      data: {error: 'OOM'},
    }));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('failed');
    expect(msg!.text).toContain('OOM');
    expect(msg!.color).toBe('red');
    expect(msg!.bold).toBe(true);
  });

  it('formats tool_call', () => {
    const msg = formatTaskEvent(event('tool_call', {
      data: {tool_name: 'read_file'},
    }));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('read_file');
    expect(msg!.dim).toBe(true);
  });

  it('formats tool_result', () => {
    const msg = formatTaskEvent(event('tool_result', {
      data: {success: 'true'},
    }));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('✓');
  });

  it('formats task_completed', () => {
    const msg = formatTaskEvent(event('task_completed'));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('Task completed');
    expect(msg!.color).toBe('green');
  });

  it('formats task_failed', () => {
    const msg = formatTaskEvent(event('task_failed', {
      data: {error: 'timeout'},
    }));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('Task failed');
    expect(msg!.color).toBe('red');
  });

  it('formats unknown event type', () => {
    const msg = formatTaskEvent(event('custom_event'));
    expect(msg).not.toBeNull();
    expect(msg!.text).toContain('custom_event');
    expect(msg!.dim).toBe(true);
  });

  it('subtask_assigned without worker_id shows "unknown"', () => {
    const msg = formatTaskEvent(event('subtask_assigned', {data: {}}));
    expect(msg!.text).toContain('unknown');
  });

  it('subtask_failed without error data shows "unknown"', () => {
    const msg = formatTaskEvent(event('subtask_failed', {data: {}}));
    expect(msg!.text).toContain('unknown');
  });

  it('task_failed without error data shows "unknown"', () => {
    const msg = formatTaskEvent(event('task_failed', {data: {}}));
    expect(msg!.text).toContain('unknown');
  });

  it('tool_result with success=false shows ✗', () => {
    const msg = formatTaskEvent(event('tool_result', {
      data: {success: 'false'},
    }));
    expect(msg!.text).toContain('✗');
  });

  it('event with undefined subtaskId uses empty string slice', () => {
    const msg = formatTaskEvent(event('subtask_started', {subtaskId: undefined}));
    expect(msg).not.toBeNull();
    // (undefined ?? '').slice(-6) = ''.slice(-6) = ''
    expect(msg!.text).toContain('Subtask started: ');
  });

  it('eventType is preserved on all messages', () => {
    const types = ['task_submitted', 'subtask_started', 'subtask_completed', 'tool_call', 'subtask_failed'];
    for (const t of types) {
      const msg = formatTaskEvent(event(t));
      if (msg) {
        expect(msg.eventType).toBe(t);
      }
    }
  });
});

describe('formatTaskEvents', () => {
  it('converts multiple events', () => {
    const events = [event('task_submitted'), event('subtask_started')];
    const msgs = formatTaskEvents(events);
    expect(msgs).toHaveLength(2);
  });

  it('returns empty array for empty input', () => {
    const msgs = formatTaskEvents([]);
    expect(msgs).toEqual([]);
  });

  it('filters out null results', () => {
    // All valid events should produce messages, but the function
    // should still handle potential nulls gracefully
    const events = [event('task_submitted'), event('subtask_completed')];
    const msgs = formatTaskEvents(events);
    expect(msgs.length).toBeGreaterThan(0);
  });
});
