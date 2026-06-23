import {describe, it, expect} from 'vitest';
import {createUserMessage, createSystemMessage, buildProgressBar, filterMessages} from './ChatLog.js';
import type {ChatMessage} from './ChatLog.js';

// ── createUserMessage ────────────────────────────────────────

describe('createUserMessage', () => {
  it('returns message with isUser=true', () => {
    const msg = createUserMessage('hello');
    expect(msg.isUser).toBe(true);
  });

  it('sets text correctly', () => {
    const msg = createUserMessage('hello world');
    expect(msg.text).toBe('hello world');
  });

  it('generates id starting with "user-"', () => {
    const msg = createUserMessage('test');
    expect(msg.id).toMatch(/^user-/);
  });

  it('generates timestamp in HH:MM:SS format', () => {
    const msg = createUserMessage('test');
    expect(msg.timestamp).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it('produces unique ids across calls', () => {
    const msg1 = createUserMessage('a');
    const msg2 = createUserMessage('b');
    expect(msg1.id).not.toBe(msg2.id);
  });

  it('does not set color, bold, dim, or eventType', () => {
    const msg = createUserMessage('test');
    expect(msg.color).toBeUndefined();
    expect(msg.bold).toBeUndefined();
    expect(msg.dim).toBeUndefined();
    expect(msg.eventType).toBeUndefined();
  });
});

// ── createSystemMessage ──────────────────────────────────────

describe('createSystemMessage', () => {
  it('returns message with isUser=false', () => {
    const msg = createSystemMessage('done');
    expect(msg.isUser).toBe(false);
  });

  it('sets text correctly', () => {
    const msg = createSystemMessage('task completed');
    expect(msg.text).toBe('task completed');
  });

  it('generates id starting with "sys-"', () => {
    const msg = createSystemMessage('test');
    expect(msg.id).toMatch(/^sys-/);
  });

  it('generates timestamp in HH:MM:SS format', () => {
    const msg = createSystemMessage('test');
    expect(msg.timestamp).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it('produces unique ids across calls', () => {
    const msg1 = createSystemMessage('a');
    const msg2 = createSystemMessage('b');
    expect(msg1.id).not.toBe(msg2.id);
  });

  it('merges color option', () => {
    const msg = createSystemMessage('err', {color: 'red'});
    expect(msg.color).toBe('red');
  });

  it('merges bold option', () => {
    const msg = createSystemMessage('important', {bold: true});
    expect(msg.bold).toBe(true);
  });

  it('merges dim option', () => {
    const msg = createSystemMessage('detail', {dim: true});
    expect(msg.dim).toBe(true);
  });

  it('merges multiple options', () => {
    const msg = createSystemMessage('err', {color: 'red', bold: true, dim: false});
    expect(msg.color).toBe('red');
    expect(msg.bold).toBe(true);
    expect(msg.dim).toBe(false);
  });

  it('without options has undefined color/bold/dim', () => {
    const msg = createSystemMessage('plain');
    expect(msg.color).toBeUndefined();
    expect(msg.bold).toBeUndefined();
    expect(msg.dim).toBeUndefined();
  });

  it('without options has undefined eventType', () => {
    const msg = createSystemMessage('plain');
    expect(msg.eventType).toBeUndefined();
  });
});

// ── buildProgressBar ────────────────────────────────────────

describe('buildProgressBar', () => {
  it('returns empty string for zero total', () => {
    expect(buildProgressBar(0, 0)).toBe('');
  });

  it('returns full bar for completed', () => {
    expect(buildProgressBar(3, 3, 8)).toBe('████████ 100%');
  });

  it('returns empty bar for nothing completed', () => {
    expect(buildProgressBar(0, 5, 8)).toBe('░░░░░░░░ 0%');
  });

  it('returns partial bar', () => {
    expect(buildProgressBar(2, 3, 6)).toBe('████░░ 67%');
  });

  it('uses default width of 10', () => {
    expect(buildProgressBar(5, 10)).toBe('█████░░░░░ 50%');
  });

  it('handles single item', () => {
    expect(buildProgressBar(0, 1, 4)).toBe('░░░░ 0%');
    expect(buildProgressBar(1, 1, 4)).toBe('████ 100%');
  });
});

// ── filterMessages ─────────────────────────────────────────

describe('filterMessages', () => {
  const msgs: ChatMessage[] = [
    {id: '1', timestamp: '12:00', text: 'user msg', isUser: true},
    {id: '2', timestamp: '12:00', text: 'task started', isUser: false, eventType: 'task_submitted'},
    {id: '3', timestamp: '12:00', text: 'subtask', isUser: false, eventType: 'subtask_started'},
    {id: '4', timestamp: '12:00', text: 'tool', isUser: false, eventType: 'tool_call'},
    {id: '5', timestamp: '12:00', text: 'fail', isUser: false, eventType: 'subtask_failed'},
  ];

  it('returns all for "all" filter', () => {
    expect(filterMessages(msgs, 'all')).toHaveLength(5);
  });

  it('filters by task events', () => {
    const result = filterMessages(msgs, 'task');
    expect(result).toHaveLength(2); // user msg + task_submitted
    expect(result[1]?.eventType).toBe('task_submitted');
  });

  it('filters by subtask events', () => {
    const result = filterMessages(msgs, 'subtask');
    expect(result).toHaveLength(3); // user msg + subtask_started + subtask_failed
  });

  it('filters by tool events', () => {
    const result = filterMessages(msgs, 'tool');
    expect(result).toHaveLength(2); // user msg + tool_call
  });

  it('filters by error events', () => {
    const result = filterMessages(msgs, 'error');
    expect(result).toHaveLength(2); // user msg + subtask_failed
  });
});
